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
2. 首次启用真实 OCR 时，在项目目录打开 PowerShell：

   ~~~powershell
   python -m venv .venv
   .\.venv\Scripts\Activate.ps1
   python -m pip install --upgrade pip
   python -m pip install -r requirements-ocr.txt
   ~~~

3. 双击 start_windows.bat，或在 PowerShell 中运行：

   ~~~powershell
   .\.venv\Scripts\python.exe app.py
   ~~~

4. 浏览器打开 http://127.0.0.1:8765。

基础演示模式只使用 Python 标准库，不需要安装第三方依赖。页面中的“运行示例流程”会演示合规、不合规和待复核三种结果。

上传接口会先创建任务，再交给后台并行 Worker 处理；默认 4 个 Worker，可在 PowerShell 中按机器性能调整：

~~~powershell
$env:AGENT_WORKERS = "8"
python app.py
~~~

建议先用真实发票样本压测，再决定 Worker 数量。系统会在每一步保存任务状态，单张失败不会中断整批处理。

## 财务合规审核 Skill

审核规则保存在 `policy.json`，可以按公司的制度修改。当前默认规则如下：

- 真实上传必须具备发票号码、开票日期、销售方、购买方和价税合计；字段缺失、OCR 置信度低于 0.85、未完成税务查验，或缺少业务用途/审批/付款凭证时，结果为“待复核”。
- 价税合计不超过 3000 元，且所有合规条件满足时，才允许自动登记；超过 3000 元转财务复核；超过 10000 元转财务负责人审批，不因金额大而自动判定不合规。
- 发票查验无效、作废、疑似虚假或篡改，号码重复，金额、税额与价税合计误差超过 0.02 元，或支出与企业经营活动无关时，结果为“不通过”。
- 建议报销金额按 `min(价税合计、费用类别剩余额度、本次报销单预算余额)` 计算；未提供额度数据时不擅自截断，交由财务确认。业务招待费的企业所得税税前扣除额另按 `min(实际发生额 × 60%、年度销售收入 × 0.5%)` 计算，不等同于员工报销上限。
- 红字/负数发票进入人工复核，并要求关联原蓝字发票；增值税进项抵扣与报销登记分开审核。

目前系统尚未接入全国增值税发票查验平台和财务系统，因此真实上传任务默认会在“未提供外部查验/业务证据”时进入人工复核；后续可通过 `/api/process/{任务ID}` 传入 `tax_verification` 和 `claim_context`。

规则依据：

- [发票管理办法（国务院令第764号）](https://neimenggu.chinatax.gov.cn/nmgzzqswj/msxxgkml_19393/cfsswj/hleswj_22876/202311/t20231122_786392.html)
- [企业所得税税前扣除凭证管理办法（税务总局公告2018年第28号）](https://www.chinatax.gov.cn/chinatax/n810341/n810765/n3359382/201806/c3730094/content.html)
- [发票管理办法实施细则（税务总局令第56号）](https://fgk.chinatax.gov.cn/zcfgk/c100011/c5221006/content.html)
- [全国增值税发票查验平台](https://inv-veri.chinatax.gov.cn/?p=51)

## 启用真实 OCR

项目已固定 Windows CPU 版 PaddlePaddle 3.2.0 和 PaddleOCR 3.7.0，并包含文档/表格解析依赖。首次运行真实 OCR 时会自动下载识别模型，模型缓存默认保存在当前 Windows 用户的 `%LOCALAPPDATA%\InvoiceReimbursementAgent` 中。这样可以避免项目目录缓存由其他账号创建后产生权限错误；如果该目录不可写，程序会自动回退到项目目录的 `.cache`。


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
