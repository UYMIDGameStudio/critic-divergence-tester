"""Shared review profiles; identifiers are stable persisted API values."""

from copy import deepcopy

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


DISCIPLINE_FOCUS = {
    "general": "先根据作者明确的问题、材料和论证任务定位文章；跨学科主张分别说明适用标准，不从关键词、语言或标题猜定学科。",
    "social-science": "辨别个人、组织和制度层次；检查跨层推断的机制、比较单位与竞争解释。只有作者提出识别或因果主张时才要求相应识别依据。",
    "natural-science": "核对理论模型、观测或实验对象及其尺度和边界；区分推导假设、测量结果与外推，不把数学成立当成经验适用。",
    "engineering": "以文章实际声称的性能、可靠性、安全性或理论性质为验收对象；核对基线公平性和失败条件。没有组件增益主张时不机械要求消融。",
    "humanities": "优先追踪文本、版本、史料与解释步骤；比较原文允许的竞争读法，区分描述与价值论证。不得套用抽样代表性、显著性或随机实验作为通用门槛。",
}

RESEARCH_TYPE_FOCUS = {
    "unspecified": "研究类型尚未确认。先说明文章试图完成的论证任务及判断依据，对不确定的方法适用性保留条件，不生成该类型必缺某项的结论。",
    "empirical": "先辨别量化、质性、比较个案或混合材料。围绕实际推断检查材料选择、测量/解释过程和不确定性；实证不等于随机实验。",
    "theoretical": "以概念区分、论证步骤、解释增量或证明目标为中心。要求针对主张的理由和反例回应；不把缺少数据集、实验、统计指标或公开代码本身当作缺陷。",
    "review": "先区分叙述性、系统性、范围性或其他综述。仅在声称系统检索、可复现筛选或完整覆盖时要求对应检索与纳排证据；叙述性综述仍须交代选材与综合范围。",
    "engineering": "区分设计说明、系统评测、算法证明与部署经验，按已声称结果检查证据。实验、消融、公开代码和硬件信息只在相应主张需要时适用。",
}

METHOD_CHECKS_BY_TYPE = {
    "unspecified": ["定位研究问题、材料类型和作者希望成立的推断；先确认类型，再选择有根据的方法检查。"],
    "empirical": ["按实际研究设计核对观察单位、材料选择和分析步骤；定量测量、质性编码或个案选择各按其实际用途检查。",
                  "因果主张需检查识别假设和竞争解释；描述性主张应核对统计口径与外推范围，不强加因果设计。",
                  "对结论所依赖的误差、缺失、稳健性或解释分歧指出可核查影响；材料访问可采用受限途径，不要求公开受保护数据。"],
    "theoretical": ["追踪关键概念在不同段落中的含义、主张与理由之间的步骤，辨别定义、推导、解释与规范判断。",
                    "针对文章自身任务选择最有力的反例或竞争读法，说明它究竟挑战哪一步；不得把换一种术语重复原主张当作独立支持。",
                    "仅对实际依赖的文本、史料、案例、形式证明或外部事实要求可核查依据；理论稿不默认需要数据/代码可获得性声明。"],
    "review": ["根据综述实际声称的类型核对选材依据、时间/学科/语言范围及遗漏对结论的影响。",
               "若声称系统性、可复现筛选或完整覆盖，核对检索、纳排和筛选证据；不得把这些格式要求直接套到所有叙述性综述。",
               "检查来源之间的冲突、质量差异和综合推断，区分引用一项结论与验证该结论。"],
    "engineering": ["将需求或性质主张对应到测试、证明、系统设计或运行记录；指出证据覆盖不到的失败条件。",
                    "如主张优于基线，核对比较条件、数据切分和评测口径；如归因于某组件，才检查隔离其贡献的证据。",
                    "按复核已声称结果的实际需要核对环境和材料访问；不能凭缺少某项通用清单字段判定系统无效。"],
}


def academic_protocol(critic: str, *, discipline: str = "general", research_type: str = "unspecified") -> dict:
    """Select confirmed scope without mutating persisted protocol templates."""
    if critic not in ACADEMIC_PROTOCOLS or discipline not in DISCIPLINES or research_type not in RESEARCH_TYPES:
        raise ValueError("学术审查维度、学科或研究类型无效")
    protocol = deepcopy(ACADEMIC_PROTOCOLS[critic])
    protocol["confirmed_scope"] = {"discipline": discipline, "research_type": research_type}
    protocol["discipline_focus"] = DISCIPLINE_FOCUS[discipline]
    protocol["research_type_focus"] = RESEARCH_TYPE_FOCUS[research_type]
    protocol["applicability"] = (
        "先复述本文的具体问题、核心主张与材料，再选择适用标准。每项问题说明所针对的原文、"
        "推理步骤、成立条件和对本文结论的影响；关键词缺席、写作体例差异或另一学科惯例本身不构成缺陷。"
        "准确保留原文语言、限定词、否定和引述归属；译述不能替代可核对原文。"
    )
    if critic == "academic_methods":
        protocol["checks"] = list(METHOD_CHECKS_BY_TYPE[research_type])
    return protocol


def profile_critics(profile: str = "document") -> tuple[str, ...]:
    if profile not in PROFILES:
        raise ValueError("未知审查类型")
    return PROFILES[profile]
