"""sandbox —— 沙箱 seam 角色包的归属层（中间包）。

本层能力：**只包装 argv 的沙箱 seam**。契约（`contract`）留在骨架、平台后端在
`providers/sandbox`、消费方是 `capabilities.shell.provider`，装配在 `app.assembly`——
与受管范围 seam（`miniharness.process`）同一分工。

沙箱与受管范围是**两个独立 seam**，不得混做：沙箱回答「拿什么 argv、在什么环境下跑」，
受管范围回答「这棵进程树怎么等、怎么终止」。所以本包的契约里没有任何终止动词。

不放实现；契约语义见 `miniharness/sandbox/contract`。
"""
