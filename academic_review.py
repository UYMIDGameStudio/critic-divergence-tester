"""Conservative, offline academic prechecks, not peer-review verdicts."""

from __future__ import annotations

import re

from document_review_model import DocumentBlock, ReviewContext, StructuredDocument


def academic_prechecks(critic: str, document: StructuredDocument, context: ReviewContext):
    """Yield (anchor, finding kwargs); every heuristic stays unverified."""
    blocks = [block for block in document.blocks if block.text.strip()]
    if not blocks:
        return
    first = blocks[0]
    text = document.plain_text

    def issue(block: DocumentBlock, check: str, message: str, action: str, *, data=None):
        return block, {
            "check_id": "academic." + check, "check_data": data or {},
            "issue": message, "standard": "本地预检只报告文本线索；学术结论需独立证据与人工核验",
            "consequence": "读者可能无法追踪或复核结论；也可能有附件或不同写作结构，需要作者确认",
            "suggested_action": action, "verification_state": "cannot-confirm",
            "uncertainties": ["未核验全文语义、外部来源或研究原始材料"],
            "observation": "请定位实际论证或提供可核验材料，不能仅增加关键词即宣称研究有效",
            "owner": "作者/研究负责人",
        }

    if critic == "academic_argument":
        causal = re.compile(r"导致|因果|决定了|\bcaus(?:e[sd]?|al(?:ity)?)\b", re.I)
        for block in blocks:
            if causal.search(block.text):
                yield issue(block, "argument.causal_bridge", "发现因果措辞，请核对识别依据与竞争解释", "指出支持因果方向的独立证据、识别假设和可排除的竞争解释；证据只支持相关时收窄结论")
        if not re.search(r"局限|适用范围|边界条件|反例|竞争解释|\blimitations?\b|\bcounterexamples?\b", text, re.I):
            yield issue(first, "argument.boundaries", "未识别到适用边界或反例讨论线索", "注明主张适用范围、最强反例和能区分竞争解释的观察；已有讨论请提供定位")

    elif critic == "academic_methods":
        requirements = {
            "empirical": (("sampling", r"样本|抽样|\bsampl(?:e|es|ing)\b", "样本与选择机制"), ("measurement", r"测量|操作化|\bmeasur\w+\b|\boperational\w*\b", "测量与操作化"), ("uncertainty", r"置信区间|不确定性|标准误|\bconfidence interval\b|\buncertainty\b", "效应不确定性")),
            "theoretical": (("concepts", r"定义|界定|概念|\bconcept\w*\b|\bdefin\w*\b", "概念界定"), ("alternatives", r"反例|竞争|替代解释|\bcounterexample\w*\b|\balternative\w*\b", "竞争解释与反例")),
            "review": (("search", r"检索|数据库|\bsearch\w*\b|\bdatabases?\b", "检索范围与检索式"), ("criteria", r"纳入|排除|纳排|\binclusion\b|\bexclusion\b", "纳入与排除标准"), ("quality", r"质量评估|偏倚|\bquality assessment\b|\bbias\b", "来源质量评估")),
            "engineering": (("baseline", r"基线|对照|\bbaselines?\b", "对照基线"), ("ablation", r"消融|\bablation\b", "组件贡献与消融依据"), ("environment", r"运行环境|硬件|软件版本|\benvironment\b|\bhardware\b", "运行环境与复现条件")),
        }
        if context.research_type == "unspecified":
            yield issue(first, "methods.research_type", "尚未确认研究类型，未套用专属方法检查", "在独立审查时明确实证、理论、综述或工程类型，并解释方法选择；如需改变固定上下文，请新建项目")
        for key, pattern, label in requirements.get(context.research_type, ()):
            if not re.search(pattern, text, re.I):
                yield issue(first, "methods." + context.research_type + "." + key, "未识别到方法说明线索：" + label, "补充或定位" + label + "；不适用时说明理由")
        if not re.search(r"数据可用|代码可用|材料可用|数据获取|保密|隐私|\b(?:data|code|materials?) availability\b|\bprivacy\b", text, re.I):
            yield issue(first, "methods.availability", "未识别到材料可获得性或访问限制说明", "说明支撑结论的材料/数据/代码如何访问；如受隐私或授权限制，说明限制及可复核替代途径")

    elif critic == "academic_citations":
        reference_start = next((index for index, block in enumerate(blocks) if re.match(r"^\s*(?:#{1,6}\s*)?(?:参考文献|参考书目|references|bibliography)\s*[:：]?\s*$", block.text, re.I)), None)
        if reference_start is None:
            yield issue(first, "citations.reference_section", "未识别到独立参考文献表；脚注或其他引注体例需人工核对", "提供参考文献表，或注明采用的脚注/尾注体例并逐项核验来源与正文主张")
            return
        body, references = blocks[:reference_start], blocks[reference_start + 1:]
        entries = {}
        for block in references:
            for match in re.finditer(r"(?m)^\s*[\[［](\d{1,5})[\]］]\s*\S", block.text):
                entries.setdefault(int(match.group(1)), []).append(block)
        for number, matches in entries.items():
            if len(matches) > 1:
                yield issue(matches[1], f"citations.duplicate:{number}", f"参考文献编号 [{number}] 重复", "为不同条目分配唯一编号，并检查正文对应关系", data={"number": number})
        # Deliberately only support single numeric markers, not author-date or ranges.
        for block in body:
            for number in dict.fromkeys(int(m.group(1)) for m in re.finditer(r"[\[［](\d{1,5})[\]］]", block.text)):
                if number not in entries:
                    yield issue(block, f"citations.missing:{number}", f"正文编号 [{number}] 未找到对应文献条目", "补充真实条目或修正编号；不能生成虚构的作者、DOI 或页码", data={"number": number})
