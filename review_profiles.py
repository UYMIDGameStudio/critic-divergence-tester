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
        "objective": "逐条追踪主张—支持材料或前提—推理桥梁—适用边界，不将重复主张当作支持。",
        "checks": ["区分描述、解释、因果与规范主张，查找循环论证和层级跳跃", "检验核心主张的最强相关异议；经验争议比较可区分观察，概念、演绎或规范争议比较推导与理由", "结论是否超出材料、样本、时空与理论适用边界"],
        "evidence": "定位主张、支持材料或前提的 block；解释其如何支持结论及对相关替代解释的影响。未交代推理桥梁先标待核实；明确逻辑冲突须指出步骤，不替作者补造论证。",
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
        "checks": ["按实际引注方式核对正文与来源；有参考文献表时逐项对应，也允许脚注或嵌入链接等归属方式", "原文页码/段落、直接引文准确性、二手转引是否标明", "区分来源存在与支持主张，记录支持/不支持/冲突/无法核验", "保留 DOI/URL/附件定位、核验材料与未解决事实"],
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


# These are a curated synthesis, not a universal ranking of papers. Source URLs
# document the review method; they never verify a manuscript's factual claims.
ACADEMIC_QUALITY_VERSION = 2
ACADEMIC_QUALITY_SOURCES = {
    "plos": {"title": "PLOS ONE — Criteria for Publication", "url": "https://journals.plos.org/plosone/s/criteria-for-publication"},
    "harvard-question": {"title": "Harvard College Writing Center — Asking Analytical Questions", "url": "https://writingcenter.fas.harvard.edu/asking-analytical-questions"},
    "harvard-ethics": {"title": "Harvard — A Guide to Writing in Ethical Reasoning 15", "url": "https://writingproject.fas.harvard.edu/file_url/156"},
    "harvard-counter": {"title": "Harvard College Writing Center — Counterargument", "url": "https://writingcenter.fas.harvard.edu/counterargument"},
    "harvard-paragraph": {"title": "Harvard College Writing Center — Anatomy of a Body Paragraph", "url": "https://writingcenter.fas.harvard.edu/anatomy-body-paragraph"},
    "pmla": {"title": "PMLA — Author Instructions", "url": "https://www.cambridge.org/core/journals/pmla/information/author-instructions"},
    "uva": {"title": "University of Virginia Writing Center — Literary Analysis", "url": "https://writingcenter.virginia.edu/literary-analysis"},
    "apa-qualitative": {"title": "APA Qualitative Psychology — Submission Guidelines", "url": "https://www.apa.org/pubs/journals/qua/submit"},
    "prisma": {"title": "PRISMA 2020 statement", "url": "https://link.springer.com/article/10.1186/s13643-021-01626-4"},
    "prisma-scr": {"title": "PRISMA-ScR Checklist — conditional critical appraisal items 12 and 16", "url": "https://www.prisma-statement.org/s/PRISMA-ScR-Fillable-Checklist_11Sept2019.pdf"},
    "sanra": {"title": "SANRA — a scale for the quality assessment of narrative review articles", "url": "https://link.springer.com/article/10.1186/s41073-019-0064-8"},
    "asa": {"title": "ASA Statement on Statistical Significance and P-Values — official summary", "url": "https://www.amstat.org/asa/files/pdfs/p-valuestatement.pdf"},
    "neurips": {"title": "NeurIPS Paper Checklist", "url": "https://neurips.cc/public/guides/PaperChecklist"},
    "acm": {"title": "ACM SIGSIM-PADS 2024 — Reproducibility and Artifact Evaluation", "url": "https://sigsim.acm.org/conf/pads/2024/blog/artifact-evaluation/"},
}

ACADEMIC_QUALITY_CRITERIA = {
    "academic_argument": [
        {"id": "question-contribution", "quality": "问题明确，贡献能相对既有认识说清楚。",
         "when": "作者提出知识、解释、概念或实践上的贡献时。",
         "test": "定位问题、已有解释与本文答案；说明读者接受本文后新增、修正或更可靠地确认了什么。把作者声称的新颖性与已核验的新颖性分开。",
         "guard": "重复研究、阴性结果、澄清和严谨综合也可有贡献；不得以名刊、热度或缺少‘首次’判劣，未检索文献不能断言没有创新。",
         "sources": ["harvard-question", "pmla", "plos"]},
        {"id": "concept-warrant", "quality": "概念含义及其变化可追踪，证据到结论之间有充分理由。",
         "when": "文章从材料、定义、前提或事实推出另一项主张时。",
         "test": "列明主张依赖的关键前提，定位最薄弱的一步；检查同词异义、必要/充分条件、个体/总体、描述/价值的转换。说明是哪一步不成立以及为何影响结论。",
         "guard": "可合理恢复的省略、已说明的定义调整或历史语义变化不自动是漏洞；概念不必转为数字变量，立场差异不等于内部矛盾。",
         "sources": ["harvard-ethics"]},
        {"id": "alternatives", "quality": "重要反例与竞争解释得到实质回应。",
         "when": "有理由支持的竞争解释、概念反例或规范异议会改变核心结论时。",
         "test": "选最强且相关的异议，比较观察、文本解释、推导或价值前提的分歧；再查作者已有回应。思想实验可检验概念，不必假装是已发生的事实。",
         "guard": "不得编造反例事实，不要求穷尽一切异议；假设反例须标明是假设，不能把反对者的声音当成作者自己的断言。",
         "sources": ["harvard-counter", "harvard-ethics"]},
        {"id": "claim-calibration", "quality": "标题、摘要与结论的强度、范围符合实际证据。",
         "when": "摘要、结果和讨论对同一发现采用不同表述，或由局部材料外推时。",
         "test": "对照相关段落的对象、时间、否定、量词和模态；区分相关/因果、统计/实际重要性、可能/必然、模型内/现实中。引用发生越界的两端。",
         "guard": "已明确标为假说或未来方向的推测不等于过度结论；p 值不表示假说为真的概率，不显著不自动证明没有效应。",
         "sources": ["plos", "asa"]},
        {"id": "reader-structure", "quality": "结构帮助目标读者理解问题、论证推进与结论。",
         "when": "组织或表述问题实际阻断理解、辨认依据或追踪结论时。",
         "test": "检查段落承担的论证任务、转折与术语指代，及图表和正文是否一致；指出读者在哪一步被误导，建议局部改动并保留原意。",
         "guard": "不强制所有文章使用 IMRaD、短句或英语写作习惯；文风偏好、非母语表达和缺少指定标题本身不构成论证缺陷。",
         "sources": ["harvard-paragraph", "pmla"]},
    ],
    "academic_methods": [
        {"id": "design-fit", "quality": "研究设计足以回答作者实际提出的问题。",
         "when": "结论依赖研究、选材、推导或评测过程时。",
         "test": "从欲成立的结论逆向追到设计与材料，说明哪项条件是必要的。将设计缺陷、报告不清和材料不可访问分别写明；未知环节要求澄清而非判定失败。",
         "guard": "不要求重做成另一类研究；报告清单齐全不证明研究有效，缺一项报告也不证明该步骤没有执行。",
         "sources": ["plos", "prisma"]},
        {"id": "auditability", "quality": "关键结果或解释有与其主张相称的复核路径。",
         "when": "他人需复核计算、材料处理、证明或系统结果时。",
         "test": "定位复核所需的版本、步骤、参数、材料与访问条件；分别记录仅已说明、材料可取得、已实际执行、结果已独立复核，不能把它们混为一谈。",
         "guard": "链接存在不代表运行成功；充分引文、语境、分析示例或受限访问可形成相称复核路径，是否足够取决于主张，不强制取得或公开全部原始材料。",
         "sources": ["acm", "neurips", "apa-qualitative"]},
    ],
    "academic_citations": [
        {"id": "source-entailment", "quality": "来源可追踪，而且真正支持被归属的具体主张。",
         "when": "文章引用外部事实、理论、数据或他人观点时。",
         "test": "分开核对书目身份、版本、原句与上下文、支持的范围；引用存在只是第一步。区分直接来源与转引，不能从标题或摘要推断全文支持。",
         "guard": "打不开来源须写无法核验；本协议的参考标准不能填充为稿件事实的 external_basis，也不能凭模型记忆填写核验通过。",
         "sources": ["plos"]},
        {"id": "fair-synthesis", "quality": "相关研究被准确比较，分歧和证据强弱得到保留。",
         "when": "作者概括领域共识、争议、空白或比较其他观点时。",
         "test": "检查引入的文献怎样共同支持综合结论；对遗漏或歪曲指控给出具体可核验来源及其改变结论的理由，辨别共同转引同一证据与独立支持。",
         "guard": "引用数量、名气和年代不等于证据质量；不凭记忆要求补引，不因观点相反就认定选引不公。",
         "sources": ["plos", "prisma"]},
        {"id": "provenance-integrity", "quality": "来源、材料归属与适用的研究诚信要求透明。",
         "when": "文章涉及他人成果、受保护材料、利益关系或需伦理审查的研究时。",
         "test": "按实际研究任务核对归属、授权及适用声明；把直接可见的遗漏、潜在风险与外部待核实事实分开，提出所需证明而非作人格指控。",
         "guard": "文本相似、异常数字或缺少声明不能单独证明抄袭、造假或违规；理论稿不自动需要实验伦理审批。",
         "sources": ["plos"]},
    ],
}

METHOD_QUALITY_BY_TYPE = {
    "unspecified": [
        {"id": "type-uncertainty", "quality": "检查标准与本文实际任务匹配。", "when": "研究类型尚未确认时。",
         "test": "根据已提供材料说明可能的论证任务及未确定之处；只提出跨类型仍成立的问题，专属方法要求保留条件。",
         "guard": "不得从标题、语言或缺少某个章节猜定研究类型后宣判缺陷。", "sources": ["plos"]},
    ],
    "empirical": [
        {"id": "selection-measurement", "quality": "材料选择、测量或编码能支撑所作推断。",
         "when": "推断依赖样本、个案、观测量或分类时。",
         "test": "核对观察单位、进入/排除机制、操作化或编码与目标概念的对应；识别选择偏差、混杂和缺失处理对该结论的实际影响。",
         "guard": "质性和目的性选材不默认追求统计代表性；只在提出因果结论时要求相应识别依据。", "sources": ["plos"]},
        {"id": "quantitative-uncertainty", "quality": "定量结果的大小、不确定性和分析选择交代充分。",
         "when": "实际使用统计检验、估计、预测或量化比较时。",
         "test": "对照效应、区间、单位与分母，核对多重比较、数据复用及探索/验证的区分；检查分析假设或合理替代设定是否会改变结论。",
         "guard": "不能用 p<0.05 代替论证，不能把 p≥0.05 当成等效或无效证明；不强制所有研究采用同一种统计框架。", "sources": ["asa", "neurips"]},
        {"id": "qualitative-interpretation", "quality": "质性解释能追溯到材料，并交代研究者立场与解释分歧。",
         "when": "实际使用访谈、观察、质性编码或解释性个案材料时。",
         "test": "追踪材料到解释与概念贡献的步骤；检查反面实例、竞争解释及研究者参与如何影响判断。编码、饱和、参与者核查或编码者一致性仅按实际方法适用。",
         "guard": "若声称解释或概念贡献，仅列主题不足；描述性研究按自身目标判断。样本小或无显著性不自动是缺陷，研究者立场可构成解释资源，不以术语缺席判错。", "sources": ["apa-qualitative"]},
    ],
    "theoretical": [
        {"id": "interpretive-warrant", "quality": "概念或解释的推进由文本、史料或明确理由支撑。",
         "when": "文章开展概念分析、文本解释、历史论证或规范论证时。",
         "test": "追踪选材和版本语境、关键解释步骤及竞争读法；规范结论的价值前提应可合理恢复、关键取舍有理由。必要时由审查者作前提替换测试，不强制作者逐一写出。",
         "guard": "不把解释多元视为错误；不得要求人文理论以实验、统计显著性或数据集证明自身。", "sources": ["uva", "harvard-ethics"]},
        {"id": "formal-proof", "quality": "形式结论在明示假设下得到充分推导。",
         "when": "作者实际提出定理、形式证明或数学性质时。",
         "test": "核对定义、量词、边界情形与关键引理依赖，区分直觉、例子、计算验证和一般性证明；分开判断数学有效性与现实适用性。",
         "guard": "概念论说不自动需要形式证明；证明失败须指出具体步骤或合法反例，不能凭难懂认定错误。", "sources": ["neurips"]},
    ],
    "review": [
        {"id": "review-selection", "quality": "综述的选材方式与其覆盖范围声明相符。",
         "when": "综合既有文献时；系统检索要求仅适用于声称系统性等对应任务。",
         "test": "核对问题、时间/语言/领域范围与选择理由；若声称系统或完整检索，追踪数据库、检索式、日期、纳排及筛选。偏倚或质量评估按综述类型、目标和结论判断是否必要。",
         "guard": "叙述性综述不自动需要 PRISMA 流程图；范围综述不一律要求偏倚评估。PRISMA 是报告指南，不是方法质量评分工具。", "sources": ["prisma", "prisma-scr", "sanra"]},
        {"id": "review-synthesis", "quality": "综合解释来源间异同，结论保留证据局限。",
         "when": "从多项文献得出共同结论时。",
         "test": "核对来源设计、适用条件与相互依赖，说明冲突如何处理；合并分析须处理可比性、异质性和偏倚，避免按显著研究篇数投票。",
         "guard": "不强制进行荟萃分析；缺少可合并材料可采用有理由的叙述综合，不能将来源结论相同当成独立重复验证。", "sources": ["prisma", "sanra", "asa"]},
    ],
    "engineering": [
        {"id": "fair-comparison", "quality": "性能或组件贡献来自可解释、公平的比较。",
         "when": "声称优于基线、改进某指标或由某组件带来收益时。",
         "test": "核对数据切分、预处理、调参预算、计算资源和度量；检查泄漏及实验变化因素。只有声称组件贡献时才要求隔离该贡献的比较。",
         "guard": "并非所有系统论文都需消融、排行榜或胜过最新模型；运行更快不自动代表效果更好。", "sources": ["neurips"]},
        {"id": "failure-envelope", "quality": "可靠性、泛化或部署主张有相应条件和失败范围。",
         "when": "作者将结果推广到其他数据、环境、规模或实际使用时。",
         "test": "把每项外推对应到测试、证明或明确假设，核对重复波动、资源约束和失败实例；区分基准有效与部署可靠。",
         "guard": "不要求覆盖所有可能环境；尚未测试的扩展若已标明为未来方向，不能当作虚假的已实现结果。", "sources": ["neurips", "acm"]},
    ],
}


def _academic_quality(critic: str, research_type: str) -> dict:
    criteria = deepcopy(ACADEMIC_QUALITY_CRITERIA[critic])
    if critic == "academic_methods":
        criteria.extend(deepcopy(METHOD_QUALITY_BY_TYPE[research_type]))
    source_ids = sorted({source for criterion in criteria for source in criterion["sources"]})
    return {
        "version": ACADEMIC_QUALITY_VERSION,
        "basis": "对代表性原始论文、审稿与写作标准的人工整理；不是所有优秀文章的穷尽特征或已验证的自动质量尺度。",
        "workflow": [
            "先重构本文的问题、主张、贡献类型和支持链；保留做得成立的部分，不以寻找错误为预设结论。只执行当前 critic 职责内的标准。",
            "按确认的 document_type 与用途区分学术稿和一般评论、政策议论；非学术论说按目标读者、实际归属方式及论证承诺检查，不默认要求学术新颖性、文献综述或参考文献表。",
            "从核心结论逆向检查关键依赖，再核对细节。按 when 选择标准，先查全文的定义、反证和限定；不要机械逐项制造 Finding。",
            "每项批评区分已见文本冲突、论证/设计缺口、报告不清、外部材料不足。先检验作者最强辩护，已被回答的批评撤回，部分成立的收窄。",
            "只有能指明原文、适用理由和结论影响的问题才进入 Findings；按影响排序，合并同源问题。不设数量指标，不输出总分或录用裁决。",
            "建议最小充分修补：澄清、收窄或补证据各自说明；只有现有主张确实依赖时才要求新增实验或研究。保留可检验的修补完成条件。",
        ],
        "criteria": criteria,
        "finding_mapping": {
            "standard": "写出适用 criterion id、具体标准及其为何适用于本文；来源 ID 不是对稿件事实的核验。",
            "evidence_and_location": "使用现有 evidence/location 引用原语言原文；跨段问题在 check_data.close_reading.context_evidence 列出相关两端。",
            "issue_and_consequence": "指出具体失败步骤与影响，区分错误、未交代、不可核验；severity 取决于影响而非措辞强硬程度。",
            "close_reading": "沿用现有 author_position/strongest_defense/why_defense_fails/repair_test/context_evidence，不新增响应字段或输出私有思维过程。",
            "zero_findings": "无有根据的问题时返回 findings=[] 和真实检查范围的 zero_finding_basis；不表示已证实全文正确。",
        },
        "sources": {source: deepcopy(ACADEMIC_QUALITY_SOURCES[source]) for source in source_ids},
        "source_limits": "这些 URL 说明标准出处，应用没有替模型访问来源或执行实验。不得把标准出处当作本文引文已核验；文献新颖性和事实真伪需要各自的来源证据。",
    }


def academic_protocol(critic: str, *, discipline: str = "general", research_type: str = "unspecified") -> dict:
    """Select confirmed scope without mutating persisted protocol templates."""
    if critic not in ACADEMIC_PROTOCOLS or discipline not in DISCIPLINES or research_type not in RESEARCH_TYPES:
        raise ValueError("学术审查维度、学科或研究类型无效")
    protocol = deepcopy(ACADEMIC_PROTOCOLS[critic])
    protocol["version"] = ACADEMIC_QUALITY_VERSION
    protocol["scholarly_quality"] = _academic_quality(critic, research_type)
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
