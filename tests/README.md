# 回归测试

两个零依赖脚本，用标准库断言，不需要装 pytest：

```bash
python tests/test_query_cache.py    # 工具层：查询结果缓存
python tests/test_app_display.py    # 展示层：表格 / 下拉 / 空态 / 行数提示
```

两个脚本都是**打印 PASS/FAIL 并以退出码表示结果**（0 = 全过），方便接 CI 或手动查看。
它们不联网、不调用模型 API：`test_query_cache` 只读本地 SQLite；
`test_app_display` 把 `app.run_agent` 换成一个假函数，只驱动展示逻辑。

## 为什么不用 pytest

项目目标之一是"零额外依赖也要能跑"。这两个脚本只用到 `sys` / `pathlib` / `json`，
换一台没装任何测试框架的机器也能直接运行。如果以后测试变多，再考虑引入。
