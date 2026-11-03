# -*- coding: utf-8 -*-
"""Tool catalogue + on-demand search for the Abaqus MCP server.

Only two tools are advertised to the client (`search_tools`, `call_tool`), so a
session pays ~200 tokens instead of the full catalogue. The catalogue below is
the search index; `handlers.py` implements each name.
"""

TOOLS = [
    {
        "name": "abaqus_status",
        "summary": "会话状态：CAE 内核是否在线、版本/模型/工作目录、作业列表",
        "args": "",
        "keywords": ["status", "状态", "连接", "会话", "session", "version", "在线"],
    },
    {
        "name": "abaqus_session",
        "summary": "会话：mode=background 独立后台内核 / gui 手动打开的 GUI（需插件）/ install_gui 安装插件",
        "args": "action=start|stop|restart|install_gui, mode=background|gui, launch=false, close=true",
        "keywords": ["start", "启动", "停止", "重启", "session", "cae", "gui", "后台"],
    },
    {
        "name": "abaqus_finish",
        "summary": "任务收尾：可选保存 .cae、关闭 ODB、结束会话（后台内核会退出；手动 GUI 只断开不关窗口）",
        "args": "save=true|false|路径, name?, close=true",
        "keywords": ["finish", "结束", "收尾", "关闭", "退出", "close", "stop", "保存"],
    },
    {
        "name": "abaqus_open",
        "summary": "打开 .cae 模型库或 .odb 结果库（自动处理中文路径/旧版本）",
        "args": "path, kind=cae|odb",
        "keywords": ["open", "打开", "载入", "cae", "odb", "模型库"],
    },
    {
        "name": "abaqus_save",
        "summary": "保存当前模型库到 .cae（可分步另存）",
        "args": "path?, name?",
        "keywords": ["save", "保存", "另存", "cae", "导出模型"],
    },
    {
        "name": "abaqus_model_new",
        "summary": "新建模型（Abaqus 无单位制，需自行保证 mm-t-s 或 m-kg-s 一致）",
        "args": "name=Model-1",
        "keywords": ["model", "模型", "新建", "new", "单位"],
    },
    {
        "name": "abaqus_model_info",
        "summary": "模型树摘要：零件/实例/材料/截面/分析步/载荷/边界/作业",
        "args": "model?",
        "keywords": ["info", "信息", "模型树", "摘要", "inspect", "检查"],
    },
    {
        "name": "abaqus_inspect",
        "summary": "模型明细：sets/surfaces 计数、loads/BCs/constraints 类型、各 part 单元类型",
        "args": "model?, mesh=true",
        "keywords": ["inspect", "检查", "明细", "sets", "集合", "surfaces", "载荷",
                     "边界", "约束", "单元类型", "网格统计"],
    },
    {
        "name": "abaqus_run_python",
        "summary": "在 CAE 内核执行任意 Abaqus Python 代码（高级/应急，返回 stdout+result）",
        "args": "code, timeout=300",
        "keywords": ["python", "脚本", "代码", "exec", "高级", "api"],
    },
    {
        "name": "abaqus_part",
        "summary": "创建零件：box / cylinder / cone 锥台 / tube 空心管 / revolve 旋转体 / shell / polygon / import",
        "args": "kind, name, model?, size | radius,height | r1,r2,height | outer,inner,height | profile,angle | points,depth | path",
        "keywords": ["part", "零件", "几何", "建模", "box", "长方体", "cylinder",
                     "圆柱", "cone", "锥台", "tube", "管", "revolve", "旋转",
                     "shell", "壳", "拉伸", "extrude", "import", "step", "导入"],
    },
    {
        "name": "abaqus_material",
        "summary": "定义材料：密度/线弹性/塑性表/热膨胀",
        "args": "name, density?, E?, nu?, plastic?=[[stress,strain],...], expansion?",
        "keywords": ["material", "材料", "弹性", "elastic", "塑性", "plastic",
                     "密度", "density", "杨氏模量", "泊松"],
    },
    {
        "name": "abaqus_section",
        "summary": "创建并指派截面（实体/壳），绑定材料与厚度",
        "args": "name, material, kind=solid|shell, thickness?, part?, assign=true",
        "keywords": ["section", "截面", "属性", "指派", "壳厚", "assign"],
    },
    {
        "name": "abaqus_instance",
        "summary": "在装配中创建零件实例，可平移/旋转",
        "args": "part, name?, model?, translate=[x,y,z], rotate={point,axis,angle}",
        "keywords": ["instance", "实例", "装配", "assembly", "平移", "旋转", "move"],
    },
    {
        "name": "abaqus_set",
        "summary": "创建几何集合/表面：按坐标点 at、包围盒 box 或 all 选择面/边/体",
        "args": "name, kind=face|edge|vertex|cell|surface, owner=assembly|part, spec, part?, instance?",
        "keywords": ["set", "集合", "表面", "surface", "选择", "选区", "region", "faces"],
    },
    {
        "name": "abaqus_mesh",
        "summary": "划分网格：全局种子尺寸 + 单元类型（C3D8R/C3D10/S4R…）+ 生成",
        "args": "part, size, elem=C3D8R?, technique=free|structured|sweep, shape=tet|hex, model?, deviation=0.1",
        "keywords": ["mesh", "网格", "划分", "种子", "seed", "单元", "element",
                     "C3D8R", "C3D10", "S4R", "加密"],
    },
    {
        "name": "abaqus_step",
        "summary": "创建分析步：static 静力 / frequency 模态 / buckle 屈曲 / explicit 显式",
        "args": "name, kind, previous=Initial, nlgeom=false, timePeriod=1, numEigen=10",
        "keywords": ["step", "分析步", "静力", "static", "模态", "frequency",
                     "屈曲", "buckle", "显式", "explicit", "动力"],
    },
    {
        "name": "abaqus_bc",
        "summary": "边界条件：encastre 固支 / pinned 铰支 / xsymm-ysymm-zsymm / displacement 指定位移",
        "args": "name, region, kind, step=Initial, values={u1..u6}",
        "keywords": ["bc", "边界", "约束", "固支", "encastre", "铰支", "对称",
                     "位移", "boundary"],
    },
    {
        "name": "abaqus_load",
        "summary": "载荷：force 集中力 / pressure 压强 / gravity 重力 / moment 弯矩 / velocity 初速度",
        "args": "name, kind, region, step, values={cf1..3|magnitude|comp1..3|v1..3}",
        "keywords": ["load", "载荷", "力", "pressure", "压强", "gravity", "重力",
                     "moment", "弯矩", "初速度", "velocity"],
    },
    {
        "name": "abaqus_coupling",
        "summary": "参考点 + Coupling：面与 RP 耦合，力/弯矩加到 RP（避免面载荷按节点翻倍）",
        "args": "name, point=[x,y,z], surface=名称或选择器, model?, kind=kinematic|distributing, dofs=[1,2,3], force={cf1..3}?, moment={cm1..3}?, step=Step-1",
        "keywords": ["coupling", "耦合", "参考点", "reference point", "rp", "刚体",
                     "约束", "弯矩", "moment", "总力"],
    },
    {
        "name": "abaqus_interaction",
        "summary": "接触/约束：general 通用接触（Std 中无摩擦）、contact 面对面（支持摩擦）、tie 绑定",
        "args": "kind=general|contact|tie, name, step, main?, secondary?, friction=0",
        "keywords": ["contact", "接触", "interaction", "tie", "绑定", "摩擦",
                     "friction", "装配接触"],
    },
    {
        "name": "abaqus_job",
        "summary": "作业：create 建作业 / write 写 inp / submit 提交求解 / status 状态 / wait 等待 / abort 终止",
        "args": "action, name, model?, cpus=1, timeout=1800",
        "keywords": ["job", "作业", "提交", "求解", "submit", "solve", "运行",
                     "排队", "inp", "计算"],
    },
    {
        "name": "abaqus_odb",
        "summary": "结果库：summary 摘要 / extrema 极值(Mises 等) / probe 节点探针 / history 历程曲线",
        "args": "action, path, step?, frame?, field=S, component=MISES, top=5, node?",
        "keywords": ["odb", "结果", "后处理", "应力", "Mises", "位移", "极值",
                     "extrema", "probe", "history", "云图"],
    },
    {
        "name": "abaqus_plot",
        "summary": "生成结果图：CAE 真实渲染 PNG（GUI 一次性进程），或后台 matplotlib 云图",
        "args": "path, out?, view=Iso, field=S, component=MISES, step?, frame?, mode=auto|cae|mpl, model?, part?",
        "keywords": ["plot", "画图", "云图", "截图", "screenshot", "png", "渲染",
                     "contour", "可视化"],
    },
    {
        "name": "abaqus_export",
        "summary": "导出：inp 输入文件 / 结果 CSV（节点或单元场）/ 报告摘要",
        "args": "kind=inp|csv|report, path, out?, field=U",
        "keywords": ["export", "导出", "inp", "csv", "报告", "report", "数据"],
    },
]

# Bilingual alias -> tool names. Chinese users type commands, not API names.
ALIASES = {
    "建模": ["abaqus_part", "abaqus_material", "abaqus_section", "abaqus_instance"],
    "几何": ["abaqus_part"],
    "长方体": ["abaqus_part"], "方块": ["abaqus_part"], "圆柱": ["abaqus_part"],
    "壳": ["abaqus_part", "abaqus_section"], "板": ["abaqus_part"],
    "导入": ["abaqus_part"], "step文件": ["abaqus_part"], "igs": ["abaqus_part"],
    "材料": ["abaqus_material"], "钢": ["abaqus_material"], "铝": ["abaqus_material"],
    "截面": ["abaqus_section"], "属性": ["abaqus_section"],
    "装配": ["abaqus_instance"], "实例": ["abaqus_instance"],
    "网格": ["abaqus_mesh"], "划分": ["abaqus_mesh"], "加密": ["abaqus_mesh"],
    "分析步": ["abaqus_step"], "静力": ["abaqus_step"], "模态": ["abaqus_step"],
    "屈曲": ["abaqus_step"], "显式": ["abaqus_step"], "动力学": ["abaqus_step"],
    "边界": ["abaqus_bc"], "约束": ["abaqus_bc"], "固支": ["abaqus_bc"],
    "载荷": ["abaqus_load"], "受力": ["abaqus_load"], "压强": ["abaqus_load"],
    "接触": ["abaqus_interaction"], "绑定": ["abaqus_interaction"],
    "求解": ["abaqus_job"], "提交": ["abaqus_job"], "计算": ["abaqus_job"],
    "后处理": ["abaqus_odb"], "应力": ["abaqus_odb"], "位移": ["abaqus_odb"],
    "云图": ["abaqus_plot"], "截图": ["abaqus_plot"], "出图": ["abaqus_plot"],
    "结果": ["abaqus_odb", "abaqus_plot"], "导出": ["abaqus_export"],
}

_CJK_SPLIT = " ,，。；;:：/|()（）[]【】\t\n"


def _tokens(text):
    text = (text or "").lower()
    out = []
    buf = ""
    for ch in text:
        if ch in _CJK_SPLIT:
            if buf:
                out.append(buf)
                buf = ""
        elif "\u4e00" <= ch <= "\u9fff":
            if buf:
                out.append(buf)
                buf = ""
            out.append(ch)
        else:
            buf += ch
    if buf:
        out.append(buf)
    return [t for t in out if t]


def search(query, top_k=6):
    q = (query or "").strip().lower()
    qtok = [t for t in _tokens(q) if len(t) > 1]
    scored = []
    for idx, tool in enumerate(TOOLS):
        score = 0.0
        name = tool["name"].lower()
        if q and q in name:
            score += 8
        for kw in tool["keywords"]:
            kwl = kw.lower()
            if q and (q == kwl or q in kwl or kwl in q):
                score += 6
            for t in qtok:
                if t in kwl or kwl in t:
                    score += 2
        if q and q in tool["summary"].lower():
            score += 3
        for t in qtok:
            if t in tool["summary"].lower():
                score += 1
        for alias, names in ALIASES.items():
            if alias in q and tool["name"] in names:
                score += 7
        if score:
            scored.append((score, -idx, tool))
    scored.sort(key=lambda x: (x[0], x[1]), reverse=True)
    picked = [t for _, _, t in scored[:top_k]]
    if not picked:
        picked = TOOLS[:top_k]
    return picked


def describe(tool):
    line = "%s — %s" % (tool["name"], tool["summary"])
    if tool["args"]:
        line += "\n  args: " + tool["args"]
    return line


def by_name(name):
    for tool in TOOLS:
        if tool["name"] == name:
            return tool
    return None
