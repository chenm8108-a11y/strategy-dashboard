# 投资策略看板 · 每日自动更新

数据源：
- 腾讯文档「日更数据」在线表格（转债低溢价轮动 / 小市值轮动 / 股债ETF轮动 / 转债摊大饼 / QDII基金池）
- 东方财富·可转债列表（可转债打新）
- 阿斯达克·新股中心（港股打新：招股日历 / 时间线 / 入场费 / 保荐人）

流水线：GitHub Actions 每日 08:00（北京时间）运行 `update_dashboard.py` 生成看板并发布到 GitHub Pages。
腾讯文档授权票据存于仓库 Secrets（`TDOC_OAUTH_ACCESS_TOKEN`），由本机 WorkBuddy 自动化定期接力刷新；
票据失效时自动降级为上次缓存数据并在看板顶部提示。

看板地址：https://chenm8108-a11y.github.io/strategy-dashboard/
