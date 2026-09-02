# 票据流 Agent

企业内部财务报销的 Windows 本地演示项目。流程对应：

~~~text
收到纸质发票 → 扫描 → OCR识别（文字 + 表格） → Skill判断报销条件
        ├─ 通过 → 录入报销名单 → 财务 → 登记（财务系统待接入）
        ├─ 不通过 → 录入不合规发票文档
        └─ 低置信度/缺字段 → 人工复核
~~~

## Windows 启动

1. 安装 Python 3.10 或更高版本。
2. 双击 start_windows.bat，或在 PowerShell 中运行：

   ~~~powershell
   python app.py
   ~~~

3. 浏览器打开 http://127.0.0.1:8765。

基础演示模式只使用 Python 标准库，不需要安装第三方依赖。页面中的“运行示例流程”会演示合规、不合规和待复核三种结果。

## 启用真实 OCR

在干净的 Python 虚拟环境中按 PaddleOCR 官方文档安装 OCR 包和对应推理引擎：

~~~powershell
python -m venv .venv
.\\.venv\\Scripts\\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -r requirements-ocr.txt
~~~

不同 Windows/Python 环境的推理引擎依赖可能不同，请以 PaddleOCR 官方安装说明为准。安装成功后重新启动 app.py，界面会自动切换为“真实 OCR”。

## 目录说明

- app.py：本地 Web 服务与 API
- invoice_agent.py：Agent 编排、SQLite 任务库和合规 Skill
- ocr_engine.py：可插拔 OCR 引擎
- finance_adapter.py：后期财务系统接入点
- policy.json：报销规则配置示例
- web/：中文界面
- data/：运行时自动生成，包含任务库、上传文件和日志数据

## 设计说明

当前版本不会连接真实财务系统。通过审核的任务会标记为“已登记”，真正接入时在 finance_adapter.py 中补充 API 或批量导入逻辑即可。

所有任务均保留状态、识别置信度和判断原因；真实 OCR 识别失败或字段缺失时转入“待复核”，不会自动通过。

