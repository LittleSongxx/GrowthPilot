"""采集测试使用小型 HTML 测试夹具；真实采集由显式命令行任务完成。"""

import gzip
import json
from pathlib import Path

import httpx
import pytest
import yaml

from backend.agent_harness.rag import ingest


def source(**extra):
    return {"id": "rider-faq", "scenario_id": "meituan_rider",
            "title": "官方报名说明", "url": "https://official.example/faq", **extra}


def response(url, content, status=200, headers=None):
    return httpx.Response(status, content=content, headers=headers,
                          request=httpx.Request("GET", url))


def test_faq_keeps_question_answer_and_qualifier_without_navigation():
    html = """<body><nav>别处广告</nav><ul><li class="faq">
      <p class="q">报名要收费吗？</p><p class="a">不收报名费。保险费用依合同约定。</p>
      </li><li class="faq"><p class="q">配车吗？</p>
      <p class="a">众包自行准备，专送请咨询当地站点。</p></li></ul></body>"""
    config = source(faq_selector=".faq", scope_note="众包与专送安排不同")
    sections = ingest._sections(html.encode(), config)
    chunks = ingest._make_chunks(sections, config, "2026-10-26", "a" * 64)
    assert len(chunks) == 2
    assert chunks[0]["section"] == "报名要收费吗?"
    assert "保险费用依合同约定" in chunks[0]["text"]
    assert "别处广告" not in str(chunks)
    assert chunks[0]["scope_note"] == "众包与专送安排不同"
    assert "众包与专送安排不同" not in chunks[0]["text"]


def test_structural_chunks_preserve_heading_and_table_column_context():
    html = """<body><div id="policy"><h2>试点地区</h2><p>仅在杭州试点。</p>
      <table><tr><th>费用项目</th><th>费率（年化）</th></tr>
      <tr><td>经营贷</td><td>以审批结果为准</td></tr></table>
      <h2>申请条件</h2><p>材料必须真实。""" + "请核对当地要求。" * 80 + "</p></div></body>"
    config = source(selector="#policy")
    chunks = ingest._make_chunks(ingest._sections(html.encode(), config), config,
                                 "2026-10-26", "b" * 64)
    assert all(len(item["text"]) <= 350 for item in chunks)
    assert chunks[0]["section"] == "试点地区"
    table = next(item for item in chunks if "经营贷" in item["text"])
    assert "费用项目 | 费率(年化) — 经营贷 | 以审批结果为准" in table["text"]
    assert "经营贷" in table["section"]
    assert chunks[-1]["section"] == "申请条件"


def test_fetch_checks_redirect_host_before_request_and_honors_robots(monkeypatch):
    visited = []

    def request(client, url, transport):
        visited.append(url)
        if url.endswith("robots.txt"):
            return response(url, b"User-agent: *\nDisallow: /private")
        return response(url, b"", 302, {"location": "https://untrusted.example/content"})

    monkeypatch.setattr(ingest, "_request", request)
    monkeypatch.setattr(ingest.time, "sleep", lambda _: None)
    with httpx.Client() as client:
        with pytest.raises(ValueError, match="outside"):
            ingest._fetch(client, "https://official.example/faq", {"official.example"}, "httpx", {})
        with pytest.raises(ValueError, match="robots.txt disallows"):
            ingest._fetch(client, "https://official.example/private", {"official.example"}, "httpx", {})
    assert not any("untrusted" in url or url.endswith("private") for url in visited)


def test_gzip_is_decoded_exactly_once():
    html = "<body>官方报名要求</body>".encode()
    transport = httpx.MockTransport(lambda request: httpx.Response(
        200, content=gzip.compress(html), headers={"content-encoding": "gzip"}, request=request))
    with httpx.Client(transport=transport) as client:
        result = ingest._request(client, "https://official.example/faq", "httpx")
    assert result.content == html


def test_fetch_preserves_non_ascii_attachment_header_bytes():
    headers = [(b"content-disposition", 'attachment; filename="租赁规则.pdf"'.encode())]
    transport = httpx.MockTransport(lambda request: httpx.Response(
        200, content=b"%PDF-example", headers=headers, request=request))
    with httpx.Client(transport=transport) as client:
        result = ingest._request(client, "https://official.example/faq", "httpx")
    assert result.content == b"%PDF-example"
    assert headers[0] in result.headers.raw


def test_collection_replaces_sources_and_excludes_failed_or_removed_snapshots(tmp_path, monkeypatch):
    manifest = tmp_path / "sources.yaml"
    output = tmp_path / "knowledge"
    configs = [source(), source(id="merchant-faq", scenario_id="meituan_merchant",
                                url="https://official.example/merchant")]

    def write_manifest(entries):
        manifest.write_text(yaml.safe_dump({"allowed_hosts": ["official.example"],
                                           "sources": entries}))

    current = {"text": "旧版报名材料必须真实。" * 20, "failed": False}

    def fetch(client, url, *args, **kwargs):
        if current["failed"]:
            raise httpx.ConnectError("upstream unavailable")
        return response(url, ("<body><p>" + current["text"] + "</p></body>").encode())

    monkeypatch.setattr(ingest, "_fetch", fetch)
    write_manifest(configs)
    first = ingest.collect(manifest, output)
    original = Path(first["documents"][0]["snapshot"])
    assert first["collected"] == 2
    current["text"] = "新版报名材料不再要求纸质复印件。" * 10
    second = ingest.collect(manifest, output, "meituan_rider")
    assert second["collected"] == 1
    assert "旧版" not in str(ingest.load_chunks(output, "meituan_rider"))
    assert "旧版" in str(ingest.load_chunks(output, "meituan_merchant"))
    assert (output / original / "raw.html").exists()
    assert ingest.collect(manifest, output, "meituan_rider")["unchanged"] == 1
    current["failed"] = True
    failed = ingest.collect(manifest, output, "meituan_rider")
    assert failed["failed"] == 1
    assert failed["documents"][0]["status"] == "stale"
    assert ingest.load_chunks(output, "meituan_rider") == []
    assert ingest.load_chunks(output, "meituan_merchant")
    write_manifest([])
    assert ingest.collect(manifest, output)["removed"] == 2
    assert ingest.load_chunks(output) == []
    assert json.loads((output / "catalog.json").read_text()) == {}


def test_missing_selector_cannot_publish_navigation_as_policy():
    with pytest.raises(ValueError, match="selector"):
        ingest._sections(b"<body><nav>Navigation</nav></body>", source(selector="article"))


def test_expired_label_outside_content_prevents_ingestion():
    html = '<body><div class="invalid-label">已失效</div><article>旧政策内容</article></body>'
    with pytest.raises(ValueError, match="invalid/superseded"):
        ingest._sections(html.encode(), source(selector="article", invalid_selector=".invalid-label"))


def test_didi_standalone_numbered_requirements_are_not_discarded():
    # 以下为官方 didi-driver-admission 来源中的 PDF 原文；
    # 特意保留兼容部首，以复现 pypdf 提取出的文本。
    requirements = [
        "（⼆）⽆犯罪记录（含交通肇事罪、危险驾驶罪、暴⼒犯罪以及其他犯罪⾏为）。",
        "（三）⽆吸毒记录",
        "（四）⽆饮酒后驾驶记录",
        "（五）未因涉嫌犯罪，处于⽴案调查、侦查、审查起诉或审判阶段。",
    ]
    html = "<body>" + "".join(f"<p>{line}</p>" for line in requirements) + "</body>"
    config = source()
    chunks = ingest._make_chunks(ingest._sections(html.encode(), config), config,
                                 "2026-10-26", "c" * 64)
    text = "\n".join(chunk["text"] for chunk in chunks)
    for requirement in requirements:
        assert ingest._text(requirement) in text


def test_source_allowlist_does_not_accept_substring_hosts():
    with pytest.raises(ValueError):
        ingest._check_url("https://official.example.attacker.example/doc", {"official.example"})
    with pytest.raises(ValueError):
        ingest._check_url("http://official.example/doc", {"official.example"})


def test_nested_table_keeps_its_own_columns_without_duplicating_inner_rows():
    from bs4 import BeautifulSoup

    html = """<table><tr><th>资质项目</th><th>图片要求</th></tr>
    <tr><td>食品经营许可证</td><td>上传清晰照片
      <table><tr><th>销售类型</th><th>资质要求</th></tr>
      <tr><td>生食类食品制售</td><td>经营项目须包含生食</td></tr>
      <tr><td>自制饮品</td><td>经营项目须包含饮品</td></tr></table>
    </td></tr></table>"""
    rows = list(ingest._html_blocks(BeautifulSoup(html, "html.parser").table))
    raw_row = "销售类型 | 资质要求 — 生食类食品制售 | 经营项目须包含生食"
    assert ("table", raw_row) in rows
    assert sum("生食类食品制售" in text for _, text in rows) == 1
    assert ("table", "资质项目 | 图片要求 — 食品经营许可证 | 上传清晰照片") in rows


def test_http_200_error_page_cannot_replace_verified_source(tmp_path, monkeypatch):
    manifest = tmp_path / "sources.yaml"
    manifest.write_text(yaml.safe_dump({
        "allowed_hosts": ["official.example"],
        "sources": [source(expected_text=["经营项目须包含生食"])],
    }))
    page = {"body": "经营项目须包含生食。" + "申请材料需真实有效。" * 20}
    monkeypatch.setattr(ingest, "_fetch", lambda client, url, *a, **k:
                        response(url, ("<body>" + page["body"] + "</body>").encode()))
    output = tmp_path / "knowledge"
    assert ingest.collect(manifest, output)["collected"] == 1
    original = ingest.load_chunks(output)[0]["content_hash"]
    page["body"] = "页面已迁移，请重新登录后访问。" * 20
    failed = ingest.collect(manifest, output)
    assert failed["failed"] == 1
    assert "Expected source text missing" in failed["failures"][0]["error"]
    assert ingest.load_chunks(output) == []
    assert any(original in f.read_text() for f in output.glob("documents/*/*/document.json"))


def test_sentence_split_never_drops_a_negation_through_character_overlap():
    negative = "不存在以浏览记录、支付意愿等因素设置不同价格。"
    text = "配送机制公开。" * 40 + negative + "请核对适用区域。" * 50
    chunks = list(ingest._split(text, 350))
    assert "".join(piece for piece, _ in chunks) == text
    assert any(negative in piece for piece, _ in chunks)
    assert not any(piece.startswith("存在以") for piece, _ in chunks)
    assert all(not partial for _, partial in chunks)


def test_long_sentence_fragments_are_explicitly_marked():
    config = source()
    chunks = ingest._make_chunks([("申请条件", "条件" * 400)], config,
                                 "2026-10-27", "d" * 64)
    assert all(chunk["partial_sentence"] for chunk in chunks)
    assert all(len(chunk["text"]) <= 350 for chunk in chunks)


def test_public_content_json_preserves_real_faq_and_source_page():
    config = source(format="json_faq", json_path="data.problem_list",
                    page_url="https://official.example/product")
    body = json.dumps({"data": {"problem_list": [
        {"question": "额度如何审批？", "answer": "<p>额度以审批结果为准，人工无法干预。</p>"}
    ]}}).encode()
    chunks = ingest._make_chunks(ingest._sections(body, config), config,
                                 "2026-10-27", "e" * 64)
    assert chunks[0]["url"] == config["page_url"]
    assert chunks[0]["fetch_url"] == config["url"]
    assert "人工无法干预" in chunks[0]["text"]
    with pytest.raises(ValueError, match="JSON content path"):
        ingest._sections(b'{"error":"unavailable"}', config)


def test_public_content_post_is_read_only_manifest_request_and_checks_robots(monkeypatch):
    calls = []

    def request(client, url, transport, **kwargs):
        calls.append((url, kwargs))
        if url.endswith("robots.txt"):
            return response(url, b"User-agent: *\nAllow: /")
        return response(url, b'{"code":0,"data":{}}')

    monkeypatch.setattr(ingest, "_request", request)
    monkeypatch.setattr(ingest.time, "sleep", lambda _: None)
    with httpx.Client() as client:
        ingest._fetch(client, "https://official.example/api/content", {"official.example"},
                      "httpx", {}, method="POST", form_data={"device_type": "pc"})
    assert calls[0][1] == {}
    assert calls[1][1] == {"method": "POST", "form_data": {"device_type": "pc"}}


def test_pdf_layout_newline_does_not_make_a_fragment_a_complete_sentence():
    text = "取得您的允许后" + "申请信息" * 75 + "\n才可以使用" + "申请信息" * 40 + "。"
    chunks = list(ingest._split(text, 350))
    assert len(chunks) > 1
    assert all(partial for _, partial in chunks)


def test_html_comments_are_not_source_text_and_pdf_car_radical_is_normalized():
    html = "<body><!--模板：如果标题为空--><p>Q1:网约车要求是什么？</p><p>驾驶员需持证。</p></body>"
    sections = ingest._sections(html.encode(), source())
    assert all("模板" not in text for _, text in sections)
    assert sections[0][0] == "Q1:网约车要求是什么?"
    assert ingest._text("滴滴⻋主") == "滴滴车主"


def test_table_faq_keeps_question_not_just_serial_number_in_every_chunk():
    html = ('<body><table><tr><th>编号</th><th>问题</th><th>答案</th></tr>'
            '<tr><td>1</td><td>驾驶员证如何办理？</td><td>' +
            '申请人需提交真实资料并参加考试。' * 40 + '</td></tr></table></body>')
    chunks = ingest._make_chunks(ingest._sections(html.encode(), source()), source(),
                                 "2026-10-27", "f" * 64)
    answer_chunks = [chunk for chunk in chunks if "申请人" in chunk["text"]]
    assert len(answer_chunks) > 1
    assert all("驾驶员证如何办理?" in chunk["section"] for chunk in answer_chunks)


def test_pdf_multilevel_headings_and_chinese_layout_words(monkeypatch):
    class Page:
        def extract_text(self):
            return "1.1.3 定金、押金支付规范\n申请人需为非在\n校学生。\n3.2.3 商家退还押金规则\n按租赁合同办理。"

    class Reader:
        pages = (Page(),)

    monkeypatch.setattr(ingest, "PdfReader", lambda _: Reader())
    sections = ingest._sections(b"%PDF-fixture", source())
    assert sections[0][0] == "1.1.3 定金、押金支付规范"
    assert "非在校学生" in sections[0][1]
    assert sections[1][0] == "3.2.3 商家退还押金规则"


def test_gazette_pdf_page_range_excludes_unrelated_regulations(monkeypatch):
    from types import SimpleNamespace

    pages = tuple(SimpleNamespace(extract_text=lambda text=text: text) for text in
                  ("无关的城市建设规定。", "上一政策结尾。\n网约车准入条件。", "实施日期及附则。", "无关的财政政策。"))
    monkeypatch.setattr(ingest, "PdfReader", lambda _: SimpleNamespace(pages=pages))
    config = source(pdf_pages=[2, 3], pdf_start_text="网约车准入条件")
    chunks = ingest._make_chunks(ingest._sections(b"%PDF-fixture", config), config,
                                 "2026-10-27", "g" * 64)
    assert all(chunk["pdf_pages"] == [2, 3] for chunk in chunks)
    assert "实施日期及附则" in str(chunks)
    assert "无关" not in str(chunks)
    assert "上一政策" not in str(chunks)
    with pytest.raises(ValueError, match="page range"):
        ingest._sections(b"%PDF-fixture", source(pdf_pages=[2, 9]))
    with pytest.raises(ValueError, match="starting text"):
        ingest._sections(b"%PDF-fixture", source(pdf_pages=[2, 3], pdf_start_text="不存在的文件"))
