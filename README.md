# 股票复盘工作台

每日复盘 + 未来大事前瞻 的个人股票信息平台。GitHub Actions 每天收盘后自动采集免费行情数据，推送成静态站点，手机/电脑打开即看。

## 功能

- **今天要处理**（置顶）：今日宏观大事、自选股解禁预警、未来 3 天高重要性事件倒计时、未完成复盘提醒
- **未来大事前瞻**（重点模块）：财经日历（CPI/PMI/LPR/美联储议息…）、限售解禁市值榜、业绩预告披露，全部带倒计时，红标预警
- **每日复盘**：五大指数快照、上证 90 日趋势图（内联 SVG）、板块涨幅榜、自选股行情（可录入成本算盈亏）
- **复盘笔记**：每日记录自动归档，本地存储
- **数据安全**：个人数据（自选/成本/笔记）只存浏览器 localStorage，支持一键导出/导入 JSON 备份

## 目录结构

```
├── index.html              # 工作台（单文件，零外部依赖）
├── collect.py              # 数据采集脚本（AKShare 免费接口）
├── .github/workflows/collect.yml  # 每日定时采集
└── data/
    ├── watchlist.txt       # 自选股池（公开数据，勿放敏感信息）
    └── *.json              # 采集输出（自动生成）
```

## 本地使用

```bash
pip install akshare pandas
python collect.py                    # 采集数据到 data/
cd 本项目目录 && python -m http.server 8080   # 启动本地服务
# 浏览器打开 http://localhost:8080
```

> 直接用浏览器双击 index.html 也能打开，此时展示内置示例数据；起本地 http 服务即可看到真实数据。

## 部署到 GitHub Pages（手机随时看）

1. 新建 GitHub 仓库（公开或私有均可，Pages 需公开）
2. 把本目录全部文件推上去：
   ```bash
   git init && git add . && git commit -m "init"
   git remote add origin https://github.com/<你的用户名>/<仓库名>.git
   git push -u origin main
   ```
3. 仓库 Settings → Pages → Source 选 `Deploy from a branch` → `main` / root → Save
4. 首次部署后约 1 分钟，访问 `https://<你的用户名>.github.io/<仓库名>/`
5. 之后每个交易日 15:10（北京时间）Actions 自动采集，几分钟后页面数据即更新；也可在 Actions 页手动 Run workflow

手机使用：浏览器打开链接 → 分享 → 添加到主屏幕，当 APP 用。

## 自定义自选股池

编辑 `data/watchlist.txt`，一行一个股票代码（东财格式，如 `600519`），采集时会自动抓取这些股票的行情。注意：该文件随仓库公开，仅放代码即可。

## 隐私与免责

- 采集的是公网公开行情数据；**个人持仓成本、复盘笔记只存你浏览器的 localStorage**，不会上传到仓库，导出备份文件请自行保管
- 平台部署后链接为公网可访问，页面不包含任何个人隐私数据
- 数据来自免费公开接口，仅供个人研究参考，不构成投资建议

## 数据来源

[AKShare](https://github.com/akfamily/akshare) 聚合的东方财富、新浪财经等免费公开接口。
