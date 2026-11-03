# Abaqus API 细节与桥接约束

## 桥接

- 内核常驻进程由 MCP 服务器启动：`abaqus cae noGUI=<pkg>/cae/cae_server.py`。
- 每次 `call_tool` 生成一小段 Python，在 CAE 内核里 exec；`result` 变量回传 JSON。
- 内核里可直接用 `mdb`、`session`、`abq`（`cae/abq_helpers.py`）。
- 非 ASCII 路径由主机侧建 NTFS 目录联接（`~/.dsh/abaqus-cad/links`），
  Abaqus C++ 层只认 ASCII，别直接改回中文路径。
- 工作目录默认 `D:\Abaqus\work`（可在 `~/.dsh/abaqus-cad.json` 改）。

## 常用对象

```python
m = mdb.models['Model-1']
a = m.rootAssembly
p = m.parts['Part-1']
inst = a.instances['Part-1-1']
```

- `p.faces` / `inst.faces` 是几何仓库；`findAt(((x,y,z),))` 返回 GeomSequence。
  **不要** `tuple()` 它，Abaqus 会报 `Feature creation failed`。
- `a.Set(faces=geom, name='X')` 可包含 instance 的 faces；`PartInstance` 没有 `.Set`。
- 压力：`m.Pressure(region=a.surfaces['Top'])`，region 必须是 Surface。
- 固支：`m.EncastreBC(name, createStepName='Initial', region=set)`。

## 易错点

| 现象 | 原因 | 处理 |
|---|---|---|
| `Feature creation failed` | 传了普通 tuple 而不是 GeomSequence | 直接传 findAt 结果 |
| `AbaqusNameError: name is empty` | `mdb.Model(name="")` | 省略 name 或给合法名 |
| 找不到面 | findAt 点不在面上 | 取面中心坐标 |
| `.cae` 打不开 | 被另一个 CAE 进程占用 / 旧版本 | 渲染前复制；旧库用 2025 重存 |
| `could not locate the pre executable` | 只装了 CAE，没有求解器 | 装 Abaqus Analysis；建模/后处理不受影响 |
| ODB `previous release` | 旧版本 ODB | `abaqus -upgrade -job new -odb old.odb` |
| 中文路径乱码 | Abaqus 内部 ANSI | 用服务器返回的联接路径 |

## 查询帮助

```text
abaqus_run_python {"code":"import abaqus\nprint(abaqus.__version__)"}
help(m.Pressure)          # 在 run_python 里 print(help(...)) 只显示到 stdout
```

`abaqus_run_python` 适合一次性探查；能落成目录工具的，优先用目录工具，
便于复用与失败定位。
