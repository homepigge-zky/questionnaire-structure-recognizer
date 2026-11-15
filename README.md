# 问卷结构识别与填写工具

在本地浏览器中读取 Word/PDF、审核历史问答、匹配已审核答案、人工填写并导出真正的 Word 文件。使用 FastAPI、SQLite、原生文档解析器和本机 Ollama；前端由后端直接提供，无需另外构建。

这是 **2026-11-14 的现有源码检查点**，不是“全部真实问卷已通过”的发布声明。原项目仍有真实问卷专项验收工作。本仓库保留当前实现，方便备份和继续开发。

## 包含内容

- Word 原生结构、物理单元格、空白填写区、合并关系及来源定位。
- PDF 原生文字提取；扫描 PDF 可以明确授权本地 OCR，默认关闭。
- 本机 Qwen 结构理解与引用校验；模型不负责编造填报答案。
- 历史 DOCX/PDF、XLSX/CSV 导入审核和问答维护。
- 适用范围、章节角色、已确认同义关系及知识冲突检查。
- 人工编辑、自动保存、版本记录、填写区域预览。
- 标准 DOCX 导出及经过位置校验的原 DOCX 模板回填。旧 DOC 使用本地 LibreOffice 转换副本；PDF 输入只导出标准 Word。
- 增量数据库迁移及不依赖真实客户文件的回归测试。

**仓库不包含**客户问卷、历史答案、业务数据库、原页图像、模型权重、本地密钥或私有验收材料。新安装的知识库为空；从 GitHub 拉取源码不会恢复本机已有业务数据。

## Windows 启动

本次验证使用 Windows 和 Python 3.12.6；智能结构理解还需要本机 Ollama。旧版 DOC 转换需要另行安装 LibreOffice，或通过 `LIBREOFFICE_PATH` 指定本地可执行文件。上述程序和模型均未打包进仓库。

在仓库根目录执行：

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python -m pip install -r requirements.txt

$env:QSR_AUTH_MODE='local'
$env:QSR_MODEL_ENABLED='true'
$env:QSR_OLLAMA_URL='http://127.0.0.1:11434'
$env:QSR_OLLAMA_MODEL='qwen3:4b-instruct-2507-q4_K_M'
$env:QSR_OCR_ENABLED='false'
$env:QSR_DATA_DIR='questionnaire_structure_recognizer/data'

.\.venv\Scripts\python -m uvicorn questionnaire_structure_recognizer.api.app:app_from_env --factory --host 127.0.0.1 --port 8200
```

浏览器打开 **http://127.0.0.1:8200/**。本地免登录模式要求客户端、Host 和 Origin 为回环地址；不要改成公网监听地址。需要密钥模式时，设置 `QSR_AUTH_MODE=key` 和仅保存在服务端的 `QSR_ACCESS_KEY`。

如果尚未安装模型，需要自行决定是否下载。以下模型下载约需数 GB 磁盘空间，运行还需要额外内存；下载时间取决于网络。本仓库不会自动执行下载：

```powershell
ollama pull qwen3:4b-instruct-2507-q4_K_M
```

Ollama 服务只供本机后端访问；程序拒绝非回环模型地址，不切换到外部模型 API。未启用或模型不可用时，保留原文读取与人工处理能力，不把未完成的智能理解显示为成功。

更多环境变量见 [配置示例](questionnaire_structure_recognizer/.env.example)。它是配置说明，不含真实密钥；直接运行 Python 时请像上例一样设置环境变量，或显式使用 uvicorn 的 `--env-file` 选项加载本地配置。

## 使用路径

1. “数据导入”上传历史资料，核对问题、答案、来源和配对状态，再明确确认入库。
2. “新建问卷”上传待填写文件，后台执行识别、校验和知识匹配。
3. 在填写页检查章节、填写位置、选项和来源，补充剩余答案；人工修改优先保留。
4. 等待显示保存完成，点击“导出 Word”，选择标准格式或可用的原模板回填。
5. 无可靠答案时保持空白；位置无法可靠验证的条目不会随意写进邻近单元格。

## 测试

```powershell
.\.venv\Scripts\python -m pytest questionnaire_structure_recognizer/tests -q
```

随仓库提供的测试使用程序生成的隔离文档、结构样本和临时数据库。涉及本地私有问卷或已填写答案的专项脚本与测试没有上传。本次源码副本的检查结果见 [发布检查](PUBLICATION_CHECKS.md)，不得将其当作整批真实问卷的验收结论。

## 数据与备份

默认数据目录为 `questionnaire_structure_recognizer/data`，可用 `QSR_DATA_DIR` 更改。数据库、原件、识别版本、知识、编辑快照和导出文件保存在本机；重启保留。

更新前停止服务并备份整个数据目录。恢复时停止服务，将完整备份还原到同一配置目录，再启动对应版本。不要只复制数据库而漏掉原件、版本和导出文件。`migrations/` 中的增量迁移由应用初始化逻辑应用；正常启动不重置数据。

## 当前边界

- 结构模型和复杂表格识别仍需真实文档核对，不能承诺任意模板全自动成功。
- Word 图片内容不会自动启用 OCR；PDF OCR 也需明确授权。
- Word 预览采用结构和来源定位，不宣称具备原页精确坐标。
- PDF 输入不能原样回填为原 PDF；其 Word 导出为标准排版。
- 受保护区域、签署、未支持的数据绑定控件和不确定填写位置须人工处理。
- GitHub 源码检查点不迁移本机业务数据，也不替代正式业务验收。
