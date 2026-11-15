# 项目规则

这是现有问卷结构识别器的源码检查点，继续复用现有接口、结构模型、知识库及导出功能。

- 无可靠知识来源，答案留空；结构模型不得生成业务答案。
- 未经明确审核的结果不能进入自动复用知识库。
- 自动处理不能覆盖人工修改或原文件的有效答案。
- 客户原文件、答案、数据库、密钥、日志和原页图像不得提交 Git；不得擅自外发。
- 原件保持只读；Word 导出必须使用已保存快照和经过验证的填写位置。
- 签字、客户审核区和位置不确定的区域不可强行自动填写。
- OCR 默认关闭，不得静默下载模型或改用外部 API。
- 数据库升级只能增量迁移，禁止启动时清空业务数据。
- 修改后运行与变更相关的测试；程序测试通过不代表真实问卷全部通过。

更详细的识别和写回约束见 `questionnaire_structure_recognizer/AGENTS.md`。

Windows 启动：

```powershell
.\.venv\Scripts\python -m uvicorn questionnaire_structure_recognizer.api.app:app_from_env --factory --host 127.0.0.1 --port 8200
```

回归测试：

```powershell
.\.venv\Scripts\python -m pytest questionnaire_structure_recognizer/tests -q
```
