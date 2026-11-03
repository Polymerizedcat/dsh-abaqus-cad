# 典型工作流（可直接照抄调用）

以下参数均为 mm-tonne-s（长度 mm，力 N，应力 MPa，密度 t/mm³）。
把 `search_tools` 的结果当权威，参数名以下表为准。

## 1. 悬臂梁静力（100×20×10 mm，左端固支，顶面 0.5 MPa）

```text
abaqus_session  {"action":"start"}
abaqus_model_new {"name":"Cantilever"}
abaqus_part     {"kind":"box","name":"Beam","size":[100,20,10],"model":"Cantilever"}
abaqus_material {"name":"Steel","density":7.85e-9,"E":210000,"nu":0.3,"model":"Cantilever"}
abaqus_section  {"name":"Sec","material":"Steel","kind":"solid","part":"Beam","model":"Cantilever"}
abaqus_instance {"part":"Beam","name":"Beam-1","model":"Cantilever"}
abaqus_mesh     {"part":"Beam","size":4,"elem":"C3D8R","model":"Cantilever"}
abaqus_step     {"name":"Step-1","kind":"static","nlgeom":false,"model":"Cantilever"}
abaqus_bc       {"name":"Fixed","kind":"encastre","step":"Initial",
                 "region":{"at":[[0,10,5]],"instance":"Beam-1"},"model":"Cantilever"}
abaqus_load     {"name":"Press","kind":"pressure","step":"Step-1","values":{"magnitude":0.5},
                 "region":{"at":[[50,10,10]],"instance":"Beam-1"},"model":"Cantilever"}
abaqus_job      {"action":"create","name":"Job-1","model":"Cantilever","cpus":4}
abaqus_job      {"action":"submit","name":"Job-1","cpus":4}
abaqus_job      {"action":"wait","name":"Job-1","timeout":600}
abaqus_odb      {"action":"extrema","path":"Job-1","field":"S","component":"MISES","top":5}
abaqus_plot     {"path":"Job-1","out":"D:/Abaqus/work/Job-1.png","field":"S","component":"MISES"}
```

解析校核：悬臂梁端部挠度 `δ = qL⁴/(8EI)`（均布压力等效线载 q·b）。
100 mm、0.5 MPa、20 mm 宽、10 mm 厚：I = b h³/12 = 1666.7 mm⁴，
δ ≈ 0.5×20×100⁴/(8×210000×1666.7) ≈ 0.36 mm。数量级不符先查单位。

## 2. 模态（同几何，左端固支）

```text
abaqus_step {"name":"Freq-1","kind":"frequency","numEigen":6,"model":"Cantilever"}
abaqus_job  {"action":"create","name":"Modal","model":"Cantilever","cpus":4}
abaqus_job  {"action":"submit","name":"Modal"}
abaqus_job  {"action":"wait","name":"Modal"}
abaqus_odb  {"action":"summary","path":"Modal"}
```

一阶弯曲频率估算 `f1 ≈ 1.875²/(2πL²)·√(EI/(ρA))`；与 ODB 的 eigenFrequency
对比，误差 > 10% 通常是约束或单位错误。

## 3. 显式冲击（刚性球撞板，可先做 CAE-only 建模）

```text
abaqus_part     {"kind":"shell","name":"Plate","size":[200,200]}
abaqus_material {"name":"Al","density":2.7e-9,"E":70000,"nu":0.33}
abaqus_section  {"name":"PlateSec","material":"Al","kind":"shell","thickness":2,"part":"Plate"}
abaqus_part     {"kind":"cylinder","name":"Ball","radius":20,"height":40}
abaqus_material {"name":"Rigid","density":7.85e-9,"E":210000,"nu":0.3}
abaqus_instance {"part":"Plate","name":"Plate-1"}
abaqus_instance {"part":"Ball","name":"Ball-1","translate":[100,100,30]}
abaqus_step     {"name":"Impact","kind":"explicit","timePeriod":0.002}
abaqus_load     {"name":"V0","kind":"velocity","step":"Initial","region":{"set":"Ball-1"},
                 "values":{"v3":-5000}}
abaqus_bc       {"name":"Edge","kind":"encastre","step":"Initial","region":{"box":[[0,0,-1],[200,0,1]],"instance":"Plate-1"}}
abaqus_interaction {"kind":"general","name":"GC","step":"Impact","solver":"exp"}
```

显式步的稳定增量由最小单元尺寸控制；网格尺寸与声速 `c=√(E/ρ)` 决定，先粗算再加密。

## 4. 面-面接触（静力）

```text
abaqus_set  {"name":"Top","kind":"surface","spec":{"at":[[50,10,10]],"instance":"Block-1"}}
abaqus_set  {"name":"Bottom","kind":"surface","spec":{"at":[[50,10,0]],"instance":"Punch-1"}}
abaqus_interaction {"kind":"contact","name":"C1","step":"Step-1",
                    "main":"Top","secondary":"Bottom","friction":0.2}
```

接触对方向：`main` 为硬面/大面，`secondary` 为从面；穿透报错时先查初始间隙。

## 5. 批量读结果

```text
abaqus_odb {"action":"summary","path":"D:/Abaqus/work/Job-1.odb"}
abaqus_odb {"action":"extrema","path":"Job-1","field":"U","component":"Magnitude","top":3}
abaqus_odb {"action":"probe","path":"Job-1","node":42}
abaqus_odb {"action":"history","path":"Job-1","output":"RF1"}
```

## 6. 端部集中力：参考点 + Coupling（推荐）

把集中力/弯矩加在面上会按节点重复计数；用 RP 耦合解决：

```text
abaqus_coupling {"name":"TipRP","point":[100,5,5],
                 "surface":{"at":[[100,5,5]],"instance":"Beam-1"},
                 "kind":"kinematic","dofs":[1,2,3],
                 "force":{"cf3":-100},"step":"Step-1"}
```

- `surface` 也可以是已存在的 surface 名（如 `"TipSurf"`）。
- 弯矩用 `"moment":{"cm3":5000}`。
- 100×10×10 mm 钢梁、端载 100 N 的解析挠度 0.192 mm；粗网格 + 耦合实测
  0.214 mm（偏差 11%），数量级正确。
- 对比：同样 100 N 直接加到端面 25 个节点上 → 约 3.4 mm（25 倍），
  这是 Abaqus `ConcentratedForce` 的语义，不是工具 bug。

## 7. 变径杆/鱼竿：一次旋转成型

4 段变径实心杆（R15×300 / R10×500 / R8×300 / R5×300，总长 1400）用
`revolve` 一次建完，不要再用 4 个圆柱布尔合并：

```text
abaqus_part {"kind":"revolve","name":"Rod","model":"Rod",
  "profile":[[0,0],[15,0],[15,300],[10,300],[10,800],[8,800],
             [8,1100],[5,1100],[5,1400],[0,1400]],"angle":360}
abaqus_mesh {"part":"Rod","size":20,"elem":"C3D10",
             "technique":"free","shape":"tet","model":"Rod"}
abaqus_inspect {"model":"Rod","mesh":true}
```

- profile 是 `[r,z]`，首尾点落在轴线上（r=0）；不用画轴线，工具会加
  construction line 并自动闭合。
- 空心管用 `kind:"tube"`（outer/inner/height），锥台用 `kind:"cone"`（r1/r2/height）。
- 旋转体 + free tet 能直接得到与视频一致的 C3D10 网格；六面体需要先分区。
