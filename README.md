# ShareBox — NAS 文本/文件分享工具

跑在飞牛 NAS（fnOS）Docker 里的轻量分享服务：任意设备通过浏览器打开网页，即可分享文本和文件，其他设备打开链接即可复制/下载。

## 功能

- 📝 **文本分享**：粘贴文本 → 生成链接 → 对方打开即可一键复制全文
- 📁 **文件分享**：点击/拖拽上传 → 生成链接 → 对方打开即可下载（大文件流式写入，不占内存）
- ⏰ **有效期**：1 小时 / 1 天 / 7 天 / 30 天 / 永久，过期自动清理
- 🔒 **可选口令**：设置 `ACCESS_CODE` 后，分享时需输入口令（查看/下载链接不需要口令，适合纯局域网家庭场景）
- 📱 响应式页面，手机、平板、电脑都能直接用

## 部署（飞牛 NAS）

### 方式一：SSH + docker compose（推荐）

把 `sharebox` 整个文件夹上传到 NAS（如 `/vol1/1000/docker/sharebox`），然后：

```bash
cd /vol1/1000/docker/sharebox
docker compose up -d --build
```

### 方式二：飞牛 Docker 图形界面

1. 把 `sharebox` 文件夹上传到 NAS；
2. 打开飞牛的「Docker」应用 → 「Compose」→ 新建项目，选择该文件夹里的 `docker-compose.yml`；
3. 启动即可。

## 使用

浏览器访问 `http://NAS的IP:8890`。

- 首页选「分享文本」或「分享文件」，填好有效期 → 生成分享链接；
- 把链接发给同一局域网内的任何设备，打开即可复制文本或下载文件。

## 配置项（docker-compose.yml 中 environment）

| 变量 | 默认 | 说明 |
|---|---|---|
| `ACCESS_CODE` | 空 | 分享口令；留空则无需口令 |
| `MAX_UPLOAD_MB` | 1024 | 单文件上传上限（MB） |

端口和数据目录在 `docker-compose.yml` 的 `ports` / `volumes` 中修改。

## 数据存储

- 所有分享内容存在 `./data` 目录（`sharebox.db` 元数据 + `files/` 内容），容器重建不丢失；
- 删除整个 `data` 目录即可清空所有分享。
