"""Conservative, offline academic prechecks, not peer-review verdicts."""

from __future__ import annotations

import re

from document_review_model import DocumentBlock, ReviewContext, StructuredDocument


_REFERENCE_HEADINGS = {
    "参考文献", "参考书目", "參考文獻", "參考書目", "引用文献",
    "references", "bibliography", "works cited", "literaturverzeichnis",
    "bibliographie", "références", "список литературы", "литература",
    "bibliographia", "fontes",
}
_COMMON_METHOD_HEADINGS = {
    "methods", "methodology", "方法", "研究方法", "方法论", "方法論",
    "methoden", "méthodes", "méthodologie", "методы", "методология", "methodus",
}
_TYPE_METHOD_HEADINGS = {
    "theoretical": {"proof", "definitions", "concepts", "argument", "概念界定", "证明", "證明", "论证", "論證", "beweis", "begriffe", "définitions", "証明", "определения", "definitiones"},
    "empirical": {"sampling", "measurement", "样本", "抽样", "測量", "测量", "échantillonnage"},
    "review": {"search strategy", "selection criteria", "检索策略", "檢索策略", "纳排标准", "納排標準"},
    "engineering": {"evaluation", "system design", "评测", "評測", "系统设计", "系統設計"},
}
_PLACEHOLDER = re.compile(r"(?:\[(?:TODO|TBD)(?:[:：][^\]\n]{1,120})?\]|TODO|TBD|待补充|待補充)[。.]?", re.I)
_CAUSAL = re.compile(r"导致|導致|决定了|決定了|\bcauses?\b|\bcaused\b|\bleads? to\b|\bresults? in\b", re.I)
_CAUSAL_NONASSERTION = re.compile(
    r"不能|无法|無法|未证明|未證明|没有证据|沒有證據|并不|並不|不导致|不導致|未必|是否|能否|"
    r"\b(?:does? not|did not|cannot|can not|no evidence|not establish|not imply|reject|rejected|whether)\b", re.I,
)
_QUOTED = re.compile(r'"[^"\n]*"|“[^”\n]*”|「[^」\n]*」|『[^』\n]*』|`[^`\n]*`')


def academic_precheck_capabilities(context: ReviewContext) -> dict:
    """Disclose rule coverage, without pretending keyword sets detect language."""
    return {
        "rule_version": "academic-local-v2",
        "research_type": context.research_type,
        "literal_cue_languages": ["zh-Hans", "zh-Hant", "en"],
        "semantic_language_detection": False,
        "semantic_completeness_checked": False,
        "negative_absence_findings": False,
        "citation_scope": "single numeric markers inside an explicitly recognized numbered bibliography scheme",
        "external_sources_checked": False,
        "notes": [
            "本地预检只检查有限的字面线索、明确待办标记和单项数字引注；未执行语言识别或完整语义审查。",
            "未命中关键词不代表文章缺少边界、方法、材料或参考文献；理论稿不默认需要实验、样本、统计指标或公开数据/代码。",
            "识别多语参考文献标题只用于定位编号区域，不等于核验多语学术论证；作者—年份、范围引用、脚注及来源真实性需独立核验。",
        ],
    }


def _heading(text: str) -> str:
    text = re.sub(r"^\s*#{1,6}\s*", "", text).strip().rstrip(":：").strip()
    return re.sub(r"^\d+(?:\.\d+)*[.)]?\s+", "", text).casefold()


def _code_block(block: DocumentBlock) -> bool:
    return block.kind in {"code", "code_block"} or block.text.lstrip().startswith(("```", "~~~"))


def _prose_blocks(blocks: list[DocumentBlock]) -> list[DocumentBlock]:
    """Markdown ingestion may split fenced examples into separate paragraphs."""
    result = []
    fence = None
    for block in blocks:
        if block.kind in {"code", "code_block"}:
            continue
        excluded = fence is not None
        for line in block.text.splitlines():
            match = re.match(r"^ {0,3}(`{3,}|~{3,})(.*)$", line)
            if match is None:
                continue
            marker, rest = match.groups()
            if fence is None:
                fence = marker
                excluded = True
            elif marker[0] == fence[0] and len(marker) >= len(fence) and not rest.strip():
                fence = None
        if not excluded and block.text.strip():
            result.append(block)
    return result


def _reference_section(blocks: list[DocumentBlock]) -> tuple[int, int] | None:
    for start, block in enumerate(blocks):
        if not _code_block(block) and _heading(block.text) in _REFERENCE_HEADINGS:
            end = len(blocks)
            for index in range(start + 1, len(blocks)):
                other = blocks[index]
                if other.kind == "heading" and (block.level is None or other.level is None or other.level <= block.level):
                    end = index
                    break
            return start, end
    return None


def academic_prechecks(critic: str, document: StructuredDocument, context: ReviewContext):
    """Yield (anchor, finding kwargs); every heuristic stays unverified."""
    blocks = _prose_blocks(document.blocks)
    if not blocks:
        return
    first = blocks[0]

    def issue(block: DocumentBlock, check: str, message: str, action: str, *, data=None, evidence=None):
        return block, {
            "check_id": "academic." + check, "check_data": {"rule_version": "academic-local-v2", **(data or {})},
            "issue": message, "standard": "本地预检只报告文本线索；学术结论需独立证据与人工核验",
            "consequence": "此线索可能影响读者追踪结论，是否构成缺陷仍需按文章实际任务核对",
            "suggested_action": action, "verification_state": "cannot-confirm",
            "uncertainties": ["未核验全文语义、外部来源或研究原始材料"],
            "observation": "请定位实际论证或提供可核验材料，不能仅增加关键词即宣称研究有效",
            "owner": "作者/研究负责人", "evidence": evidence if evidence is not None else block.text,
        }

    if critic == "academic_argument":
        references = _reference_section(blocks)
        for block in blocks[:references[0] if references else len(blocks)]:
            if _code_block(block) or block.kind == "blockquote" or block.text.lstrip().startswith(">"):
                continue
            # Quotation spans must be found before sentence splitting: in normal
            # typography the closing quote follows the sentence's full stop.
            quoted = list(_QUOTED.finditer(block.text))
            for sentence in re.finditer(r"[^。！？.!?\n]+[。！？.!?]?", block.text):
                text = sentence.group()
                if _CAUSAL_NONASSERTION.search(text) or text.rstrip().endswith(("?", "？")):
                    continue
                match = next((m for m in _CAUSAL.finditer(text) if not any(q.start() <= sentence.start() + m.start() < q.end() for q in quoted)), None)
                if match:
                    yield issue(block, "argument.causal_bridge", "发现因果措辞，请核对其限定、支持依据与竞争解释",
                                "定位此句实际声称的因果关系及其支持材料；按研究任务检查推理或识别依据，不能仅因出现因果词就判错",
                                evidence=text.strip(), data={"matched_cue": match.group(), "scope": "literal causal wording; not a missing-evidence verdict"})
                    break

    elif critic == "academic_methods":
        if context.research_type == "unspecified":
            yield issue(first, "methods.research_type", "尚未确认研究类型，未套用专属方法检查", "在独立审查时明确实证、理论、综述或工程类型，并解释方法选择；如需改变固定上下文，请新建项目")
            return
        headings = _COMMON_METHOD_HEADINGS | _TYPE_METHOD_HEADINGS.get(context.research_type, set())
        section = None
        for block in blocks:
            if _code_block(block) or block.kind == "blockquote":
                continue
            if _heading(block.text) in headings:
                section = block
            elif block.kind == "heading":
                section = None
            elif section is not None and _PLACEHOLDER.fullmatch(block.text.strip()):
                yield issue(block, "methods." + context.research_type + ".draft_placeholder", "方法相关段落仍含明确的待办占位文字",
                            "检查此处是否仍待完成；补充文章实际依赖的说明，或移除不再适用的编辑待办，不凭占位符推定研究设计无效",
                            data={"section_block_id": section.block_id, "scope": "explicit editorial placeholder"})

    elif critic == "academic_citations":
        section = _reference_section(blocks)
        if section is None:
            return
        start, end = section
        body, references = blocks[:start], blocks[start + 1:end]
        entries = {}
        for block in references:
            if _code_block(block):
                continue
            for line in block.text.splitlines():
                match = re.match(r"^\s*[\[［](\d{1,5})[\]］]\s*\S", line)
                if match:
                    entries.setdefault(int(match.group(1)), []).append((block, line))
        if not entries:
            return
        for number, matches in entries.items():
            if len(matches) > 1:
                yield issue(matches[1][0], f"citations.duplicate:{number}", f"已识别的数字参考文献编号 [{number}] 重复", "核对这些行是否为不同条目；如是，为条目分配唯一编号，并检查正文对应关系",
                            evidence=matches[1][1], data={"number": number, "entry_block_ids": [m[0].block_id for m in matches], "scope": "recognized numbered bibliography"})
        # Deliberately only support single numeric markers, not author-date or ranges.
        for block in body:
            if _code_block(block):
                continue
            for number in dict.fromkeys(int(m.group(1)) for m in re.finditer(r"(?<![A-Za-z0-9_\\])[\[［](\d{1,5})[\]］](?!\()", block.text)):
                if number not in entries:
                    yield issue(block, f"citations.missing:{number}", f"正文编号 [{number}] 在已识别的数字文献条目中未找到对应编号", "先核对实际引注体例和文献表范围；确认是文献引注后补充真实条目或修正编号，不能生成虚构的作者、DOI 或页码",
                                data={"number": number, "reference_section_block_id": blocks[start].block_id, "recognized_numbers": sorted(entries), "scope": "single numeric marker in recognized bibliography scheme"})
