"""将官方页面转为可审计的快照，再切分成保留上下文的小段政策原文。

仅抓取 sources.yaml 中列出的 URL，不递归爬取。每个来源都发布为
完整快照，并通过 catalog.json 完成原子发布。
刷新失败时保留旧快照用于审计，但旧快照不再参与检索。
"""

from __future__ import annotations

import hashlib
import io
import json
import re
import shutil
import subprocess
import tempfile
import time
import unicodedata
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urljoin, urlsplit
from urllib.robotparser import RobotFileParser

import httpx
import yaml
from bs4 import BeautifulSoup, Comment, Tag
from pypdf import PdfReader
from pypdf.errors import PyPdfError

from backend.paths import redact_local_paths

USER_AGENT = "GrowthAgentKnowledgeBot/1.0"
MAX_BYTES = 10 * 1024 * 1024
CHUNK_CHARS = 350
EXTRACTOR_VERSION = 6


def _text(value: str) -> str:
    # PDF 中的大多数兼容部首可通过 NFKC 规范化；滴滴 PDF 使用的
    # 中日韩部首 U+2ECB 需要显式映射。
    return re.sub(r"\s+", " ", unicodedata.normalize("NFKC", value).replace("⻋", "车")).strip()


def _check_url(url: str, allowed_hosts: set[str]) -> None:
    parsed = urlsplit(url)
    if (parsed.scheme != "https" or parsed.hostname not in allowed_hosts
            or parsed.username or parsed.password or parsed.port not in (None, 443)):
        raise ValueError(f"URL is outside the configured HTTPS sources: {url}")


def _request(client: httpx.Client, url: str, transport: str, *,
             method: str = "GET", form_data: dict | None = None,
             request_headers: dict | None = None) -> httpx.Response:
    if transport == "curl":
        # 少数官方站点使用 Python/OpenSSL 不支持的旧版 TLS。
        # 系统 curl 仍须校验证书；禁止使用 --insecure 或 -L。
        with tempfile.TemporaryDirectory() as folder:
            body, headers = Path(folder) / "body", Path(folder) / "headers"
            form_args = [arg for key, value in (form_data or {}).items()
                         for arg in ("--data-urlencode", f"{key}={value}")]
            header_args = [arg for key, value in (request_headers or {}).items()
                           for arg in ("--header", f"{key}: {value}")]
            result = subprocess.run(
                ["curl", "--silent", "--show-error", "--max-time", "30",
                 "--max-filesize", str(MAX_BYTES), "--user-agent", USER_AGENT,
                 "--dump-header", str(headers), "--output", str(body),
                 "--request", method, *form_args, *header_args, url],
                capture_output=True, timeout=35, check=False,
            )
            if result.returncode:
                raise RuntimeError(f"curl fetch failed: {result.stderr.decode(errors='replace')}")
            lines = headers.read_bytes().replace(b"\r\n", b"\n").strip().split(b"\n\n")[-1].splitlines()
            status = int(lines[0].split()[1])
            fields = [(key.strip(), value.strip()) for line in lines[1:] if b":" in line
                      for key, value in [line.split(b":", 1)]]
            return httpx.Response(status, headers=fields, content=body.read_bytes(),
                                  request=httpx.Request("GET", url))
    with client.stream(method, url, data=form_data, headers=request_headers) as response:
        content = bytearray()
        for part in response.iter_bytes():
            content.extend(part)
            if len(content) > MAX_BYTES:
                raise ValueError("Document exceeds the 10 MiB collection limit")
        # iter_bytes 已经解码 gzip/br，不能重复解码。
        # 保留原始字节：官方 PDF 的附件名称可能包含中文。
        headers = [(key, value) for key, value in response.headers.raw
                   if key.lower() not in {b"content-encoding", b"content-length"}]
        return httpx.Response(response.status_code, headers=headers, content=bytes(content),
                              request=response.request)


def _fetch(client: httpx.Client, url: str, allowed_hosts: set[str], transport: str,
           robots: dict, *, check_robots: bool = True, method: str = "GET",
           form_data: dict | None = None, request_headers: dict | None = None) -> httpx.Response:
    for _ in range(5):
        _check_url(url, allowed_hosts)
        origin = f"https://{urlsplit(url).netloc}"
        if check_robots:
            if origin not in robots:
                response = _fetch(client, origin + "/robots.txt", allowed_hosts, transport,
                                  robots, check_robots=False)
                parser = RobotFileParser()
                if response.status_code in (404, 410):
                    parser.parse([])
                else:
                    response.raise_for_status()
                    parser.parse(response.text.splitlines())
                robots[origin] = parser
            parser = robots[origin]
            if not parser.can_fetch(USER_AGENT, url):
                raise ValueError(f"robots.txt disallows collection: {url}")
            time.sleep(parser.crawl_delay(USER_AGENT) or 0.2)
        kwargs = {}
        if method != "GET":
            kwargs.update(method=method, form_data=form_data)
        if request_headers:
            kwargs["request_headers"] = request_headers
        response = _request(client, url, transport, **kwargs)
        if response.is_redirect:
            url = urljoin(url, response.headers["location"])
            if response.status_code in (301, 302, 303):
                method, form_data = "GET", None
            continue
        return response
    raise ValueError("Too many redirects")


def _html_blocks(node: Tag):
    """按原始顺序依次产出标题、段落和表格行，每个元素仅处理一次。"""
    if node.name == "table":
        heading = ""
        for row in node.find_all("tr"):
            if row.find_parent("table") is not node:
                continue
            # 嵌套表格有自己的列标题，不能先将其合并到父单元格中，
            # 再重复读取其中的表格行。
            cells = [_text(" ".join(str(part) for part in cell.find_all(string=True)
                                    if part.find_parent("table") is node))
                     for cell in row.find_all(["th", "td"], recursive=False)]
            line = " | ".join(cells)
            if not heading:
                heading = line
            yield "table", line if line == heading else heading + " — " + line
            for nested in row.find_all("table"):
                if nested.find_parent("table") is node:
                    yield from _html_blocks(nested)
        return
    if re.fullmatch(r"h[1-6]", node.name or ""):
        yield "heading", _text(node.get_text(" ", strip=True))
        return
    blocks = "p,li,div,section,article,table,h1,h2,h3,h4,h5,h6"
    if node.name in {"p", "li", "div"} and not node.select_one(blocks):
        yield "paragraph", _text(node.get_text(" ", strip=True))
        return
    for child in node.children:
        if isinstance(child, Tag):
            yield from _html_blocks(child)
        elif str(child).strip():
            yield "paragraph", _text(str(child))


def _sections(content: bytes, source: dict) -> list[tuple[str, str]]:
    if source.get("format") in {"json_faq", "json_html"}:
        # 部分官方页面通过公开内容接口渲染；保留接口返回的原始 JSON，
        # 只提取已声明的字段，不执行页面中的 JavaScript。
        value = json.loads(content)
        for key in source["json_path"].split("."):
            if not isinstance(value, dict) or key not in value:
                raise ValueError("Configured JSON content path was not found")
            value = value[key]
        if source["format"] == "json_faq":
            if not isinstance(value, list) or not value or any(
                not isinstance(item, dict) or not isinstance(item.get("question"), str)
                or not isinstance(item.get("answer"), str) for item in value
            ):
                raise ValueError("Configured JSON FAQ has no valid question/answer pairs")
            return [(_text(item["question"]), _text(BeautifulSoup(
                item["answer"], "html.parser").get_text(" ", strip=True))) for item in value]
        if not isinstance(value, str) or not value.strip():
            raise ValueError("Configured JSON HTML field is empty")
        content = ("<body>" + value + "</body>").encode()
    is_pdf = content.startswith(b"%PDF")
    if is_pdf:
        blocks = []
        pages = PdfReader(io.BytesIO(content)).pages
        if source.get("pdf_pages"):
            start, end = source["pdf_pages"]
            if not (type(start) is int and type(end) is int and 1 <= start <= end <= len(pages)):
                raise ValueError("PDF page range must be valid 1-based inclusive pages")
            pages = pages[start - 1:end]
        for page in pages:
            blocks.extend(("paragraph", _text(line))
                          for line in (page.extract_text() or "").splitlines())
        if source.get("pdf_start_text"):
            starts = [i for i, (_, line) in enumerate(blocks)
                      if _text(source["pdf_start_text"]) in line]
            if len(starts) != 1:
                raise ValueError("PDF starting text must match exactly one selected line")
            blocks = blocks[starts[0]:]
    else:
        soup = BeautifulSoup(content, "html.parser")
        for comment in soup.find_all(string=lambda value: isinstance(value, Comment)):
            comment.extract()
        if source.get("invalid_selector") and soup.select_one(source["invalid_selector"]):
            raise ValueError("Official page marks this document as invalid/superseded")
        for node in soup.select("script,style,nav,header,footer,noscript,svg"):
            node.decompose()
        if source.get("faq_selector"):
            sections = []
            for faq in soup.select(source["faq_selector"]):
                question, answer = faq.select_one(".q"), faq.select_one(".a")
                if question and answer:
                    sections.append((_text(question.get_text(" ", strip=True)),
                                     _text(answer.get_text(" ", strip=True))))
            if not sections:
                raise ValueError("Configured FAQ selector found no question/answer pairs")
            return sections
        root = soup.select_one(source.get("selector", "body"))
        if root is None:
            raise ValueError("Configured content selector was not found")
        blocks = list(_html_blocks(root))

    sections, lines = [], []
    heading = source["title"]

    def joined():
        value = "\n".join(lines)
        # PDF 在中文词语内部产生的排版换行不代表语义分隔。
        # (e.g. 非在\n校学生). Keep paragraph/heading structure handled below.
        return re.sub(r"(?<=[\u4e00-\u9fff])\n(?=[\u4e00-\u9fff])", "", value) if is_pdf else value

    for kind, line in blocks:
        if not line:
            continue
        if kind == "table":
            if lines:
                sections.append((heading, joined()))
                lines = []
            columns, _, row = line.partition(" — ")
            # 在每个切块的章节元数据中保留表格列名和该行主题，
            # 即使要求说明所在的单元格过长、必须拆分，也要保留。
            cells = row.split(" | ")
            subject = next((cell for cell in cells if len(cell) <= 100 and
                            ("?" in cell or "？" in cell)), cells[0][:40])
            row_heading = " / ".join([heading, columns[:70], subject])
            sections.append((row_heading, line))
            continue
        is_clause = bool(re.match(r"^第[一二三四五六七八九十百]+条", line))
        is_heading = kind == "heading" or (len(line) < 70 and re.match(
            r"^(?:[一二三四五六七八九十]+[、.．]|[（(][一二三四五六七八九十]+[）)]|Q\d+[.:：、]|\d+(?:\.\d+)+\s+)", line))
        if is_heading or is_clause:
            if lines:
                sections.append((heading, joined()))
            # 简短的编号条目也可能是一项完整要求，例如
            # “(三)无吸毒记录”。即使下一行也是标题，也必须保留该条目。
            heading, lines = line[:70], [line]
        else:
            lines.append(line)
    if lines:
        sections.append((heading, joined()))
    return sections


def _split(text: str, limit: int):
    """按句子边界切分；对超长句子拆出的片段作标记。

    不能按字符数截取重叠片段，否则可能把“不存在”截成“存在”。
    通过标题保留上下文，并尽量保持句子完整。
    """
    continued = False
    while text:
        cut, incomplete = len(text), False
        if len(text) > limit:
            # PDF 的换行是排版结果，不代表句子边界。
            cut = max(text.rfind(mark, 0, limit) for mark in "。；;！？!?") + 1
            if not cut:
                cut = max(text.rfind(mark, 0, limit) for mark in "，,、") + 1 or limit
                incomplete = True
        yield text[:cut].strip(), continued or incomplete
        continued = incomplete
        text = text[cut:].strip()


def _make_chunks(sections: list[tuple[str, str]], source: dict, fetched_at: str,
                 content_hash: str) -> list[dict]:
    chunks = []
    for heading, text in sections:
        for piece, partial_sentence in _split(text, CHUNK_CHARS):
            record = {
                "id": f"{source['id']}:{len(chunks):04d}:{content_hash[:12]}",
                "document_id": source["id"], "scenario_id": source["scenario_id"],
                "title": source["title"], "section": heading, "text": piece,
                "url": source.get("page_url", source["url"]), "fetched_at": fetched_at,
                "published_at": source.get("published_at"), "content_hash": content_hash,
            }
            if source.get("page_url"):
                record["fetch_url"] = source["url"]
            if partial_sentence:
                record["partial_sentence"] = True
            for key in ("region", "business_type", "document_type", "valid_from", "valid_until",
                        "scope_note", "pdf_pages"):
                if source.get(key) is not None:
                    record[key] = source[key]
            chunks.append(record)
    return chunks


def _write_json(path: Path, value) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def collect(sources_path: Path, output_dir: Path, scenario_id: str | None = None) -> dict:
    """刷新已配置的来源，返回统计数量并明确列出每个来源的失败情况。"""
    manifest = yaml.safe_load(sources_path.read_text())
    sources, hosts = manifest["sources"], set(manifest["allowed_hosts"])
    ids = set()
    for source in sources:
        if not re.fullmatch(r"[a-z0-9_-]+", source["id"]) or source["id"] in ids:
            raise ValueError("Source ids must be unique, lowercase path-safe identifiers")
        ids.add(source["id"])
        _check_url(source["url"], hosts)
        if source.get("page_url"):
            _check_url(source["page_url"], hosts)
        if source.get("transport", "httpx") not in ("httpx", "curl"):
            raise ValueError("Only httpx or explicitly configured curl transport is supported")
        if source.get("method", "GET") not in {"GET", "POST"}:
            raise ValueError("Only GET or explicitly reviewed read-only content POST is supported")
        if source.get("format", "html") not in {"html", "pdf", "json_faq", "json_html"}:
            raise ValueError("Unsupported source content format")
        if set(source.get("request_headers", {})) - {"language", "Accept-Language"}:
            raise ValueError("Public content sources only accept language headers, not credentials")
    selected = [s for s in sources if scenario_id is None or s["scenario_id"] == scenario_id]
    output_dir.mkdir(parents=True, exist_ok=True)
    catalog_path = output_dir / "catalog.json"
    catalog = json.loads(catalog_path.read_text()) if catalog_path.exists() else {}
    summary = {"sources": len(selected), "collected": 0, "unchanged": 0, "failed": 0,
               "removed": 0, "chunks": 0, "failures": [], "documents": []}
    for key, old in list(catalog.items()):
        if key not in ids and (scenario_id is None or old["scenario_id"] == scenario_id):
            del catalog[key]
            summary["removed"] += 1
    robots = {}
    with httpx.Client(timeout=30, headers={"User-Agent": USER_AGENT}) as client:
        for source in selected:
            now = datetime.now(UTC).isoformat()
            old = catalog.get(source["id"], {})
            try:
                response = _fetch(client, source["url"], hosts,
                                  source.get("transport", "httpx"), robots,
                                  method=source.get("method", "GET"),
                                  form_data=source.get("form_data"),
                                  request_headers=source.get("request_headers"))
                response.raise_for_status()
                raw = response.content
                sections = _sections(raw, source)
                clean = "\n\n".join(f"{heading}\n{text}" for heading, text in sections)
                if len(clean) < 80:
                    raise ValueError("No substantive text; page may require JS/login or be removed")
                # HTTP 200 也可能返回实际无内容的 404 提示页或登录页；
                # 使用各来源特有的原文锚点，防止这类响应进入索引。
                body = _text("\n".join(text for _, text in sections))
                missing = [anchor for anchor in source.get("expected_text", [])
                           if _text(anchor) not in body]
                if missing:
                    raise ValueError(f"Expected source text missing: {missing}")
                content_hash = hashlib.sha256(raw).hexdigest()
                chunks = _make_chunks(sections, source, now, content_hash)
                signature = json.dumps([source, EXTRACTOR_VERSION], sort_keys=True).encode() + raw
                snapshot = Path("documents") / source["id"] / hashlib.sha256(signature).hexdigest()
                destination = output_dir / snapshot
                if not destination.exists():
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    staging = Path(tempfile.mkdtemp(prefix=".collect-", dir=destination.parent))
                    try:
                        suffix = (".json" if source.get("format", "").startswith("json_") else
                                  ".pdf" if raw.startswith(b"%PDF") else ".html")
                        (staging / ("raw" + suffix)).write_bytes(raw)
                        (staging / "clean.txt").write_text(clean, encoding="utf-8")
                        _write_json(staging / "chunks.json", chunks)
                        _write_json(staging / "document.json", {**source, "fetched_at": now,
                                    "content_hash": content_hash, "final_url": str(response.url)})
                        staging.rename(destination)
                    finally:
                        if staging.exists():
                            shutil.rmtree(staging)
                unchanged = old.get("snapshot") == snapshot.as_posix()
                record = {"id": source["id"], "scenario_id": source["scenario_id"],
                          "status": "unchanged" if unchanged else "fresh", "snapshot": snapshot.as_posix(),
                          "fetched_at": now, "chunks": len(chunks), "last_checked_at": now}
                summary["unchanged" if unchanged else "collected"] += 1
                summary["chunks"] += len(chunks)
            except (httpx.HTTPError, ValueError, OSError, RuntimeError, PyPdfError,
                    subprocess.SubprocessError) as exc:
                error = redact_local_paths(f"{type(exc).__name__}: {exc}")
                record = {**old, "id": source["id"], "scenario_id": source["scenario_id"],
                          "status": "stale", "fetch_error": error, "last_checked_at": now}
                summary["failed"] += 1
                summary["failures"].append({"id": source["id"], "url": source["url"], "error": error})
            catalog[source["id"]] = record
            summary["documents"].append(record)
    temporary = output_dir / ".catalog.json.tmp"
    _write_json(temporary, catalog)
    temporary.replace(catalog_path)
    return summary


def load_chunks(output_dir: Path, scenario_id: str | None = None) -> list[dict]:
    """仅读取当前目录中完整且刷新成功的来源。"""
    catalog_path = output_dir / "catalog.json"
    if not catalog_path.exists():
        return []
    chunks = []
    for entry in json.loads(catalog_path.read_text()).values():
        if entry["status"] == "stale" or (scenario_id and entry["scenario_id"] != scenario_id):
            continue
        for chunk in json.loads((output_dir / entry["snapshot"] / "chunks.json").read_text()):
            chunks.append({**chunk, "fetched_at": entry["fetched_at"],
                           "source_status": entry["status"]})
    return chunks
