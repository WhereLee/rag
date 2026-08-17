"""
自造语料生成器：白皮书（表格+插图）、技术规范（条款+参数表）、扫描件、独立图片。

设计原则：所有事实均为确定性可出题内容（数字/日期/人名/列表），
作为黄金评估集的 ground truth 来源。
"""
import sys
from pathlib import Path

import pymupdf

CORPUS = Path(__file__).resolve().parents[1] / "data" / "corpus"

WHITEPAPER_HTML = """
<h1>企业智能文档管理白皮书（2026年）</h1>
<p><b>发布单位：</b>蓝图数字研究院　<b>发布日期：</b>2026年3月18日　<b>编号：</b>LT-2026-017</p>
<h2>第一章 调研概况</h2>
<p>本白皮书基于对 1283 家企业的实地调研，覆盖制造、金融、政务、教育、医疗五大行业，
系统梳理了智能文档管理系统的落地现状与技术趋势。调研由蓝图数字研究院联合三家行业协会完成，
问卷回收有效率为 91.2%。</p>
<p>调研显示，67% 的受访企业已经部署或正在部署智能文档问答系统，
较 2025 年的 55% 有显著提升。其中制造业部署率最高，达到 72.4%，
医疗行业部署率最低，为 49.3%。</p>
<h2>第二章 部署现状</h2>
<p>分行业部署率数据如下表所示。制造业由于工艺文档与质检报告体量大，
成为智能文档管理的第一落地场景；金融行业紧随其后，主要驱动力是合规审计的自动化需求。</p>
<table>
<tr><th>行业</th><th>部署率</th><th>主要驱动因素</th></tr>
<tr><td>制造业</td><td>72.4%</td><td>工艺文档与质检报告管理</td></tr>
<tr><td>金融</td><td>68.9%</td><td>合规审计自动化</td></tr>
<tr><td>政务</td><td>61.2%</td><td>公文流转与归档</td></tr>
<tr><td>教育</td><td>55.7%</td><td>教学资料检索</td></tr>
<tr><td>医疗</td><td>49.3%</td><td>病历结构化（推进较慢）</td></tr>
</table>
<p>试点示范方面，研究院在苏州、佛山、东莞三座城市设立了应用试点，
项目负责人为陈静博士。试点运行一年后的评估显示，
三座城市合计节省文档处理人力成本约 4.2 亿元。</p>
<h2>第三章 技术成熟度</h2>
<p>文档解析平均准确率已达到 98.5%，端到端问答平均时延为 2.3 秒，
幻觉率从上一年的 7.8% 降至 3.1%。三项关键指标的改善主要归功于
混合检索与精排技术的普及，以及生成后自检机制的引入。</p>
<table>
<tr><th>技术方案</th><th>平均召回率</th><th>平均时延</th><th>单次成本指数</th></tr>
<tr><td>传统关键词检索</td><td>61.3%</td><td>0.4 秒</td><td>1.0</td></tr>
<tr><td>向量检索</td><td>74.6%</td><td>0.8 秒</td><td>1.6</td></tr>
<tr><td>混合检索+精排</td><td>86.2%</td><td>1.5 秒</td><td>2.4</td></tr>
<tr><td>Agentic RAG</td><td>91.7%</td><td>4.2 秒</td><td>5.8</td></tr>
</table>
<p>技术路线演进呈现清晰的三阶段特征：2024 年以向量检索为主，
2025 年混合检索与精排成为主流，2026 年 Agentic RAG 开始规模化落地。
白皮书判断，未来两年检索质量评估器与人工审批闭环将成为企业级系统的标配能力。</p>
<h2>第四章 趋势与建议</h2>
<p>部署率趋势方面，2022 年至 2026 年的部署率依次为 31%、42%、55%、67%、78%（预测），
保持年均约 12 个百分点的增长。</p>
<p>白皮书建议企业在选型时重点关注四项能力：一是多格式解析能力，
尤其是表格与扫描件的处理质量；二是评估体系建设，任何优化都应以黄金评估集数据为准；
三是反馈闭环机制，将用户负反馈转化为回归测试用例；四是人机边界设计，
关键变更必须经过人工审批。</p>
<h2>第五章 典型案例分析</h2>
<p>案例一：苏州某装备制造企业接入智能文档问答系统后，
工艺文件检索耗时从平均 15 分钟降至 40 秒，一线工程师日均提问 6.3 次，
其中 78% 的问题在首轮回答中获得采纳。该企业文档总量约 42 万卷，
采用混合检索加精排方案，建库周期 11 天。</p>
<p>案例二：佛山某政务服务中心将公文归档流程接入系统后，
归档差错率从 2.7% 降至 0.4%，年度审计准备时间缩短 60%。
该中心特别肯定了拒答机制的价值：系统对库外问题明确回复未找到，
避免了工作人员被错误信息误导。</p>
<p>案例三：东莞某医疗器械企业的合规文档审核场景对幻觉率要求极高，
通过生成后自检与引用标注双重机制，将合规问答幻觉率控制在 1.2%，
低于本规范一级系统 2% 的要求。</p>
<h2>第六章 风险与挑战</h2>
<p>调研同时揭示了三项主要风险：一是解析质量参差，
约 23% 的企业反馈扫描件与复杂表格的解析结果不达预期；
二是评估缺失，超过半数企业没有任何量化评估手段，优化依赖主观感受；
三是权限与审计不完善，敏感文档的访问控制普遍薄弱。
针对上述风险，白皮书建议在项目立项阶段即引入本白皮书第三章所列指标体系，
并将第四章四项能力作为供应商选型门槛。</p>
<p>（本白皮书由蓝图数字研究院版权所有，引用请注明出处。）</p>
"""

STANDARD_HTML = """
<h1>文档智能解析技术规范（LT/S 001—2026）</h1>
<p><b>标准编号：</b>LT/S 001—2026　<b>实施日期：</b>2026年7月1日　<b>归口单位：</b>蓝图数字研究院</p>
<h2>1 范围</h2>
<p>本规范规定了企业级文档智能解析系统的术语定义、技术要求、性能指标与验收方法，
适用于以问答为目的的文档入库与检索系统建设。</p>
<h2>2 术语和定义</h2>
<p>2.1 检索块（chunk）：文档切分后用于向量化与检索的最小文本单元。</p>
<p>2.2 混合检索：向量语义检索与关键词检索并行执行并融合排序的检索方式。</p>
<p>2.3 精排（rerank）：对初筛结果使用交叉编码器重新打分排序的过程。</p>
<h2>3 切块技术要求</h2>
<p>3.1 检索块长度上限为 500 字，超出部分应按句子边界二分。</p>
<p>3.2 相邻检索块重叠长度应为 60 字，以保证跨块语义连续。</p>
<p>3.3 表格应整体作为独立检索块，超过 800 字的表格按行分组拆分且每组保留表头。</p>
<p>3.4 每个检索块应注入来源上下文行，至少包含文档标题与页码。</p>
<h2>4 检索与生成指标</h2>
<table>
<tr><th>指标项</th><th>一级系统要求</th><th>二级系统要求</th></tr>
<tr><td>系统可用率</td><td>99.9%</td><td>99.5%</td></tr>
<tr><td>问答端到端时延（P95）</td><td>≤ 3 秒</td><td>≤ 6 秒</td></tr>
<tr><td>检索召回率（黄金集）</td><td>≥ 90%</td><td>≥ 80%</td></tr>
<tr><td>幻觉率</td><td>≤ 2%</td><td>≤ 5%</td></tr>
<tr><td>拒答准确率</td><td>≥ 95%</td><td>≥ 85%</td></tr>
</table>
<p>4.1 向量相似度阈值：低于 0.62 的检索结果不得直接作为答案依据。</p>
<p>4.2 精排相关性判定：rerank 分数低于 -5 的结果应判为不相关并剔除。</p>
<p>4.3 对于知识库中不存在答案的问题，系统应明确拒答，禁止编造。</p>
<h2>5 运维与审批</h2>
<p>5.1 提示词（prompt）变更必须经过回归评估与人工审批，审批响应时限不超过 48 小时。</p>
<p>5.2 用户负反馈应在 7 日内完成归因分析，确认为系统缺陷的应转入回归测试集。</p>
<p>5.3 索引重建应安排在业务低峰期，重建期间旧索引须保持可用。</p>
"""

NOTICE_TEXT = """蓝 图 数 字 研 究 院 办 公 室 文 件

蓝办〔2026〕12 号

关于开展历史档案数字化试点工作的通知

各相关部门：
  为提升档案检索效率，经院务会研究决定，启动历史档案数字化试点工作。
现将有关事项通知如下：
  一、试点范围：馆藏 1998 年至 2015 年期间的纸质档案，共计约 30 万卷。
  二、完成时限：2026 年 12 月 31 日前完成全部扫描与质检。
  三、项目预算：总预算 180 万元，由信息化建设专项列支。
  四、质量要求：扫描分辨率不低于 300 DPI，抽检合格率不低于 99%。
  五、责任部门：档案管理部牵头，信息技术部提供系统支持。

  特此通知。

                    蓝图数字研究院办公室
                    2026 年 5 月 20 日
"""


def build_story_pdf(html: str, out_path: Path, page_size=(595, 842)):
    """用 Story API 将 HTML 排成多页 PDF。"""
    story = pymupdf.Story(html=html, user_css="""
        body { font-family: sans-serif; font-size: 14px; line-height: 1.5; }
        h1 { font-size: 22px; text-align: center; }
        h2 { font-size: 17px; margin-top: 16px; }
        p { margin: 10px 0; text-align: justify; }
        table { border-collapse: collapse; margin: 12px 0; }
        th, td { border: 1px solid #333; padding: 6px 12px; font-size: 13px; }
        th { background: #e8e8e8; }
    """)
    writer = pymupdf.DocumentWriter(str(out_path))
    mediabox = pymupdf.Rect(0, 0, page_size[0], page_size[1])
    where = mediabox + (56, 64, -56, -64)  # 页边距
    more = True
    while more:
        dev = writer.begin_page(mediabox)
        more, _ = story.place(where)
        story.draw(dev)
        writer.end_page()
    writer.close()
    print(f"[gen] {out_path.name}: {pymupdf.open(out_path).page_count} pages")


def draw_arch_figure(doc: pymupdf.Document, page_idx: int):
    """在白皮书指定页绘制架构图（box 图 + 标注）。"""
    page = doc[page_idx]
    rect = page.rect
    x0, y0 = rect.width * 0.15, rect.height * 0.35
    w, h = rect.width * 0.7, 40
    labels = ["文档解析层（PDF/图片/扫描件）", "混合检索层（向量+BM25+精排）",
              "Agent 编排层（路由/纠错/反思）", "生成与审批层（LLM+人工闭环）"]
    colors = [(0.85, 0.92, 1), (0.85, 1, 0.88), (1, 0.95, 0.8), (1, 0.85, 0.88)]
    for i, (label, color) in enumerate(zip(labels, colors)):
        y = y0 + i * (h + 22)
        r = pymupdf.Rect(x0, y, x0 + w, y + h)
        page.draw_rect(r, color=(0.2, 0.2, 0.2), fill=color, width=1)
        page.insert_textbox(r, label, fontsize=12, align=1,
                            fontname="china-s")
        if i < len(labels) - 1:
            page.draw_line((x0 + w / 2, y + h), (x0 + w / 2, y + h + 22),
                           color=(0.3, 0.3, 0.3), width=1.5)
    page.insert_text((x0, y0 - 24), "图 1 智能文档问答系统总体架构",
                     fontsize=11, fontname="china-s")


def draw_trend_figure(doc: pymupdf.Document, page_idx: int):
    """绘制部署率趋势柱状图。"""
    page = doc[page_idx]
    rect = page.rect
    base_x, base_y = rect.width * 0.18, rect.height * 0.72
    chart_w, chart_h = rect.width * 0.64, 140
    years = ["2022", "2023", "2024", "2025", "2026(预)"]
    values = [31, 42, 55, 67, 78]
    max_v = 100.0
    bar_w = chart_w / (len(years) * 1.8)
    page.draw_line((base_x, base_y), (base_x + chart_w, base_y), width=1)
    page.draw_line((base_x, base_y), (base_x, base_y - chart_h), width=1)
    for i, (yr, v) in enumerate(zip(years, values)):
        cx = base_x + chart_w * (i + 0.5) / len(years)
        bh = chart_h * v / max_v
        r = pymupdf.Rect(cx - bar_w / 2, base_y - bh, cx + bar_w / 2, base_y)
        page.draw_rect(r, color=(0.2, 0.3, 0.6), fill=(0.55, 0.7, 0.95), width=1)
        page.insert_text((cx - 14, base_y + 14), yr, fontsize=9, fontname="china-s")
        page.insert_text((cx - 8, base_y - bh - 6), f"{v}%", fontsize=9)
    page.insert_text((base_x, base_y - chart_h - 16),
                     "图 2 2022—2026 年智能文档系统部署率趋势",
                     fontsize=11, fontname="china-s")


def build_scanned_pdf(out_path: Path):
    """文本 PDF → 渲染为图片 → 重组为无文本层 PDF（模拟扫描件）。"""
    # 第一步：用 Story 生成临时文本 PDF
    tmp = out_path.with_suffix(".tmp.pdf")
    html = "<body>" + "".join(f"<p style='font-size:14px'>{line}</p>"
                               for line in NOTICE_TEXT.splitlines() if line.strip()) + "</body>"
    story = pymupdf.Story(html=html, user_css="body{font-family:sans-serif;}")
    writer = pymupdf.DocumentWriter(str(tmp))
    mediabox = pymupdf.Rect(0, 0, 595, 842)
    more = True
    while more:
        dev = writer.begin_page(mediabox)
        more, _ = story.place(mediabox + (70, 80, -70, -80))
        story.draw(dev)
        writer.end_page()
    writer.close()

    # 第二步：逐页渲染 200DPI 图片，重建为纯图片 PDF
    src = pymupdf.open(tmp)
    out = pymupdf.open()
    for page in src:
        pix = page.get_pixmap(dpi=200)
        imgpdf = pymupdf.open()
        p = imgpdf.new_page(width=page.rect.width, height=page.rect.height)
        p.insert_image(p.rect, pixmap=pix)
        out.insert_pdf(imgpdf)
        imgpdf.close()
    out.save(out_path)
    out.close()
    src.close()
    try:
        tmp.unlink()
    except PermissionError:
        print(f"[gen] warn: tmp file locked, left behind: {tmp.name}")
    print(f"[gen] {out_path.name}: scanned (image-only)")


def build_standalone_images(out_dir: Path):
    """独立图片语料：架构图 PNG + 趋势图 PNG。"""
    for name, drawer in (("system_architecture.png", draw_arch_figure),
                         ("deployment_trend.png", draw_trend_figure)):
        doc = pymupdf.open()
        doc.new_page(width=595, height=842)
        drawer(doc, 0)
        pix = doc[0].get_pixmap(dpi=150)
        pix.save(str(out_dir / name))
        doc.close()
        print(f"[gen] image: {name}")


def main():
    wp_dir = CORPUS / "whitepaper"
    std_dir = CORPUS / "standard"
    scan_dir = CORPUS / "scanned"
    img_dir = CORPUS / "image"
    for d in (wp_dir, std_dir, scan_dir, img_dir):
        d.mkdir(parents=True, exist_ok=True)

    # 白皮书（HTML 排版 → 插图）
    wp_path = wp_dir / "企业智能文档管理白皮书（2026年）.pdf"
    build_story_pdf(WHITEPAPER_HTML, wp_path)
    doc = pymupdf.open(wp_path)
    draw_arch_figure(doc, 1)
    draw_trend_figure(doc, doc.page_count - 1)
    doc.saveIncr()
    doc.close()

    # 技术规范
    build_story_pdf(STANDARD_HTML, std_dir / "文档智能解析技术规范 LT-S 001-2026.pdf")

    # 扫描件 + 独立图片
    build_scanned_pdf(scan_dir / "历史档案数字化试点通知（扫描件）.pdf")
    build_standalone_images(img_dir)
    print("[gen] corpus generation done")


if __name__ == "__main__":
    main()
