"""Shared review profiles; identifiers are stable persisted API values."""

DOCUMENT_CRITICS = (
    "expression_ambiguity", "execution_feasibility", "compliance_legal_screen",
    "reasonableness_governance", "official_professional_format",
)
ACADEMIC_CRITICS = ("academic_argument", "academic_methods", "academic_citations")
ALL_CRITICS = DOCUMENT_CRITICS + ACADEMIC_CRITICS
PROFILES = {"document": DOCUMENT_CRITICS, "academic": ACADEMIC_CRITICS, "mixed": ALL_CRITICS}
CRITIC_LABELS = dict(zip(ALL_CRITICS, (
    "表达清晰度", "执行可行性", "合规风险筛查", "治理合理性", "正式规范性",
    "学术论证与反例", "研究方法与可复现性", "引用与证据核验",
)))
DISCIPLINES = {"general": "通用/跨学科", "social-science": "社会科学", "natural-science": "自然科学", "engineering": "工程研究", "humanities": "人文研究"}
RESEARCH_TYPES = {"unspecified": "待确认", "empirical": "实证研究", "theoretical": "理论/解释研究", "review": "文献综述", "engineering": "工程/系统研究"}

ACADEMIC_PROTOCOLS = {
    "academic_argument": {
        "role": "学术论证与反例审查者",
        "objective": "逐条追踪主张—独立证据—推理桥梁—适用边界，不将重复主张当作支持。",
        "checks": ["区分描述、解释、因果与规范主张，查找循环论证和层级跳跃", "对核心主张给出最强竞争解释、反例和可区分观察", "结论是否超出材料、样本、时空与理论适用边界"],
        "evidence": "定位主张和支持材料的 block；解释证据如何支持此主张、排除了什么以及排除理由。缺少桥梁只标待核实，不替作者补造论证。",
        "exclusions": "不以多数意见判真，不把文风差异写成逻辑错误，不输出总分。",
    },
    "academic_methods": {
        "role": "研究方法与可复现性审查者",
        "objective": "按确认的学科和研究类型核对方法与结论是否匹配，不给理论文章强套实验指标。",
        "checks": ["实证：样本与选择机制、测量操作化、识别假设、混杂、缺失数据、效应量与不确定性", "理论/人文：概念界定、材料选择理由、解释步骤、竞争读法与反例", "综述：检索范围、纳排标准、筛选流程、质量评估及综合边界", "工程：需求、基线、数据切分、消融、失败场景和运行环境", "材料/数据/代码的可获得性；隐私或伦理限制应明确说明，不要求公开受保护数据"],
        "evidence": "每个缺口注明适用研究类型、原文定位、对结论的影响和最小补充材料。无法判断研究类型时请求作者确认。",
        "exclusions": "不伪造实验、数据、统计结果或伦理审批，不把关键词命中当作方法有效。",
    },
    "academic_citations": {
        "role": "引用与证据核验审查者",
        "objective": "分别检查来源是否存在、书目信息是否准确、来源是否支持归属的主张。",
        "checks": ["正文引用与参考文献逐项对应，定位缺失与重复条目", "原文页码/段落、直接引文准确性、二手转引是否标明", "区分来源存在与支持主张，记录支持/不支持/冲突/无法核验", "保留 DOI/URL/附件定位、核验材料与未解决事实"],
        "evidence": "任何核验结论须有 external_basis 的来源名、定位和 URL/附件及适用说明；仅见参考文献或模型记忆不能宣称 verified。没有原文时输出 cannot-confirm。",
        "exclusions": "不编造作者、DOI、页码或原文；不把引用数量当作质量分数。",
    },
}


def profile_critics(profile: str = "document") -> tuple[str, ...]:
    if profile not in PROFILES:
        raise ValueError("未知审查类型")
    return PROFILES[profile]
