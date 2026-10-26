# 从一条题目理解题集

四个场景分别有 100 道 RAG 题和 100 个动态客户案例，合计 800 个案例。对话默认每例运行 2 次；重复运行仍是同一个案例。

| 场景 | 检索题 | 动态客户 |
| --- | --- | --- |
| 美团骑手 | [meituan_rider.json](rag/meituan_rider.json) | [meituan_rider.json](dialogue/meituan_rider.json) |
| 美团商家 | [meituan_merchant.json](rag/meituan_merchant.json) | [meituan_merchant.json](dialogue/meituan_merchant.json) |
| 滴滴司机 | [didi_driver.json](rag/didi_driver.json) | [didi_driver.json](dialogue/didi_driver.json) |
| 度小满 | [financial_consultation.json](rag/financial_consultation.json) | [financial_consultation.json](dialogue/financial_consultation.json) |

`corpus/` 保存 4 个场景的完整冻结语料，共 1,285 个片段，既包含答案片段，也包含干扰项。来源及版本规则见[语料说明](corpus/README.md)。题目依据这份快照，不代表今天仍有效的政策或品牌内部资料。

## RAG 题怎么看

先读 `question`，再看 `relevant_chunk_ids` 与 `evidence`。后者记录文档 ID、内容哈希和原文引文，可以回到对应场景的语料逐字核对。

相关的定义是：片段正文能够直接支持问题中至少一个必答点。只含主题标题、泛泛介绍或相邻但无关的条款不算相关；不同文档里的等价条款和互补条款都可以相关。这是片段级相关性标注，不能把一个知识点的重复条款当成多个独立知识点。所有相关片段共同组成 Recall 的分母，不为适配检索结果或 k 值删减证据。

`topic` 便于查找业务主题，`question_family` 用于识别相近问题。分块或语料版本变化后，旧片段 ID 和标注可能失效，必须重新核对，不能直接沿用原成绩。

## 动态客户案例怎么看

`customer` 描述客户的背景、顾虑、接受条件、拒绝条件和信息披露规则。配置的 OpenAI 兼容模型根据这些设定和实际对话生成下一句话，没有预先写好的逐轮台词。

`goal` 描述本案例合理的成功条件，仅供对话结束后的裁判读取。目标分为推进、解答咨询和尊重拒绝；不要求所有客户都被转化。`source_case_ids` 连接对应检索题，方便核对业务依据，但不会提供给业务 Agent。`max_turns` 只限制运行长度。

## 标注记录与维护

`annotations/<场景>-author.json` 记录初次编写范围；`-review.json` 记录另一个子 agent 从问题与完整语料独立复标的结果；`-adjudication.json` 记录两者分歧、依据原文作出的取舍和最终证据集合。原始复标保留，不用最终答案覆盖它。

这些是模型辅助编写、独立复标并仲裁的参考集，尚无真实通话样本，也未经业务专家人工确认。后续发现题意、适用范围或引文错误，应直接修正题集并记录原因；比较新旧成绩时应使用同一份题集和语料。客户案例的分布由教学覆盖需要设计，不能据此推断真实业务人群比例。

运行命令与指标口径见[评测入口](../README.md)。
