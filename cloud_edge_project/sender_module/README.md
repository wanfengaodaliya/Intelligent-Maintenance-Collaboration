# 发送器模块

该目录是按照 `模块架构/发送器模块整体框架.txt` 重写的独立发送器，不依赖旧 `project2.0`。

## 运行条件

- Python 3.10+
- Mosquitto监听 `127.0.0.1:1883`
- 调度器提供 `POST http://127.0.0.1:8003/scheduler/decide`

安装Python依赖：

```powershell
python -m pip install -r requirements.txt
```

## 本地完整测试

终端一启动模拟调度器：

```powershell
python tools/mock_scheduler.py
```

终端二启动测试订阅器：

```powershell
python tools/test_subscriber.py
```

终端三实时回放一条MAT记录：

```powershell
python -m sender --config config/local.json --mat-file "..\KA01\N09_M07_F10_KA01_1.mat"
```

正常情况下约4秒发布80包。只做快速功能测试时可以增加 `--accelerated`，但加速结果不能作为真实系统时延。

## 本地日志

每包最终发送记录：`runtime/logs/packet_logs.jsonl`

任务汇总：`runtime/logs/task_logs.jsonl`

每行都是一条独立JSON。未来日志服务上线后可以将同样的记录批量发送到HTTP接口，本地文件继续作为失败兜底。

## 网络仿真

当前不在业务代码中伪造网络延迟。网络模块完成后，只把 `scheduler_url`、`mqtt_host` 和 `mqtt_port` 改成对应代理地址。

## 自动化测试

```powershell
python -m unittest discover -s tests -v
```
