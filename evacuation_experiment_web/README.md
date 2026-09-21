# 火灾疏散地图信任实验

## 运行

在此目录中执行：

```powershell
py -3 -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
python app.py
```

访问 `http://127.0.0.1:5000`。

## 素材与量表

项目直接使用父目录的正式材料：`基线测试.png`、`无拓扑错误.png`、`轻度拓扑错误.png`、`重度拓扑错误.png`，并将 AI/人工来源标签与三种错误程度随机组合。量表条目、三个火灾情境和出口选项集中在 `experiment/config.py`。

## 研究人员后台

访问 `/researcher` 查看完成情况，访问 `/researcher/export.csv` 下载逐被试汇总数据，访问 `/researcher/revisits.csv` 下载每次地图重看事件。默认口令仅供本机开发：`change-me`。正式部署时请设置环境变量 `RESEARCHER_PASSWORD`。
