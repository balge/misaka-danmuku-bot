不维护了，Misaka基本功能都自带了。

# Misaka Danmaku Bot

Telegram 机器人指令与 Misaka Danmaku API 的对接

## 快速部署

### 使用 Docker Compose（推荐）

1. 创建 `docker-compose.yml` 文件：

```yaml
version: "3.8"

services:
  # Telegram弹幕机器人服务
  misaka-danmaku-bot:
    image: ghcr.io/balge/misaka-danmuku-bot:latest
    container_name: misaka-danmaku-bot
    restart: unless-stopped
    network_mode: host

    # 数据持久化配置
    volumes:
      - ./app:/app/app # 持久化配置和日志数据

    # 环境变量配置
    environment:
      # Telegram机器人必填配置
      - TELEGRAM_BOT_TOKEN=机器人token，botfather获取
      - ALLOWED_USER_IDS=用户id，多个用逗号分隔，get My Id 获取（第一个用户默认为管理员）
      # 选填，如果想多个管理员，需要这里设置，普通用户直接在bot中命令添加或者ALLOWED_USER_IDS添加
      - ADMIN_USER_IDS=管理员用户id，多个用逗号分隔，拥有完整功能权限

      # Misaka Danmaku API必填配置
      - DANMAKU_API_BASE_URL=http://127.0.0.1:7768/api/control
      - DANMAKU_API_KEY=外部apikey

      # 代理配置（可选）
      - HTTP_PROXY=http://127.0.0.1:2083
      - HTTPS_PROXY=http://127.0.0.1:2083
      - NO_PROXY=localhost,127.0.0.1

      # TMDB配置（可选，用于智能搜索辅助）
      - TMDB_API_KEY=your_tmdb_api_key_here
      # TMDB代理URL（可选，用于通过代理访问TMDB API）
      - TMDB_PROXY_URL=https://your-tmdb-proxy.com

      # TVDB配置（可选，用于TVDB链接解析）
      - TVDB_API_KEY=your_tvdb_api_key_here

      # BGM配置（可选，用于Bangumi链接解析）
      - BGM_ACCESS_TOKEN=your_bgm_access_token_here

      # Webhook配置（可选，用于接收媒体服务器通知）
      - WEBHOOK_PORT=7769
      - WEBHOOK_API_KEY=自定义的Webhook密钥
      - WEBHOOK_CALLBACK_ENABLED=true

      # 其他可选配置
      - API_TIMEOUT=60
      - LOG_LEVEL=INFO
      - ENVIRONMENT=production
```

2. 启动服务：

```bash
docker-compose up -d
```

### 数据持久化

机器人使用 `app` 目录存储配置文件和日志，支持数据持久化：

```
app/
├── config/            # 配置文件目录
│   └── user.json      # 用户权限配置（自动生成）
└── logs/              # 日志文件目录
    └── app.log        # 应用日志
```

**重要说明：**

- 用户权限变更会自动保存到 `user.json`
- 通过 Docker 卷映射可实现数据持久化
- 重启容器后配置和用户数据不会丢失

### Webhook 自动入库/更新

- `WEBHOOK_PORT=7769`
- `WEBHOOK_API_KEY=自定义的Webhook密钥`
- `WEBHOOK_CALLBACK_ENABLED=true`

emby 添加播放通知（http://ip:WEBHOOK_PORT/api/webhook/emby?api_key=WEBHOOK_API_KEY）

**重要说明：**

添加一下参数，有助识别准确度

- `TMDB_API_KEY`: TMDB API 密钥（用于智能搜索辅助，从 https://www.themoviedb.org/settings/api 获取）
- `TVDB_API_KEY`: TVDB API 密钥（用于 TVDB 链接解析和媒体信息获取，从 https://thetvdb.com/api-information 获取）
- `BGM_ACCESS_TOKEN`: Bangumi API 访问令牌（用于 BGM 链接解析和媒体信息获取，从 https://bgm.tv/dev/app 创建应用获取）

### 环境变量说明

#### 必填配置

- `TELEGRAM_BOT_TOKEN`: Telegram 机器人 Token
- `ALLOWED_USER_IDS`: 允许使用机器人的用户 ID（多个用户用逗号分隔）
- `ADMIN_USER_IDS`: 管理员用户 ID（多个用户用逗号分隔，拥有完整功能权限）
- `DANMAKU_API_BASE_URL`: Misaka Danmaku API 基础地址
- `DANMAKU_API_KEY`: Misaka Danmaku API 密钥

#### 可选配置

- `HTTP_PROXY`: HTTP 代理地址
- `HTTPS_PROXY`: HTTPS 代理地址
- `NO_PROXY`: 不使用代理的地址列表
- `API_TIMEOUT`: API 请求超时时间（秒，默认 60）
- `LOG_LEVEL`: 日志级别（INFO/DEBUG/WARNING/ERROR，默认 INFO）
- `ENVIRONMENT`: 运行环境（development/production，默认 production）
- `DEBUG`: 调试模式（true/false，默认 false）
- `TMDB_API_KEY`: TMDB API 密钥（用于智能搜索辅助，从 https://www.themoviedb.org/settings/api 获取）
- `TMDB_PROXY_URL`: TMDB 代理 URL（可选，用于通过代理访问 TMDB API）
- `TVDB_API_KEY`: TVDB API 密钥（用于 TVDB 链接解析和媒体信息获取，从 https://thetvdb.com/api-information 获取）
- `BGM_ACCESS_TOKEN`: Bangumi API 访问令牌（用于 BGM 链接解析和媒体信息获取，从 https://bgm.tv/dev/app 创建应用获取）
- `WEBHOOK_PORT`: Webhook 监听端口（默认 7769）
- `WEBHOOK_API_KEY`: Webhook API 密钥（用于验证请求来源）-- 使用方式同御坂通知，端口改成 WEBHOOK_PORT 自定义的即可
- `WEBHOOK_CALLBACK_CHAT_ID`: Webhook 消息通知用户 id，默认第一个管理员账号
- `WEBHOOK_CALLBACK_ENABLED`: Webhook 消息是否通知（默认 true）
- `TELEGRAM_CONNECT_TIMEOUT`: Telegram 连接超时时间（秒，默认 30）
- `TELEGRAM_READ_TIMEOUT`: Telegram 读取超时时间（秒，默认 30）
- `TELEGRAM_POOL_TIMEOUT`: Telegram 连接池超时时间（秒，默认 30）
- `TELEGRAM_CONNECTION_POOL_SIZE`: Telegram 连接池大小（默认 50）

### 本地开发

1. 克隆项目：

```bash
git clone https://github.com/balge/misaka-danmuku-bot.git
cd misaka-danmuku-bot
```

2. 复制配置文件：

```bash
cp .env.example .env
```

3. 编辑 `.env` 文件，填入必要的配置

4. 安装依赖并运行：

```bash
pip install -r requirements.txt
python bot.py
```

## 权限说明

### 管理员权限

管理员用户（在 `ADMIN_USER_IDS` 中配置）拥有完整的功能权限：

- ✅ 媒体搜索和导入 (`/search`)
- ✅ 自动导入功能 (`/auto`)
- ✅ URL 链接导入 (`/url`)
- ✅ 刷新数据源 (`/refresh`)
- ✅ Token 管理 (`/tokens`)
- ✅ 用户权限管理 (`/users`)
- ✅ Webhook 黑名单管理 (`/blacklist`)
- ✅ 自定义识别词管理 (`/identify`)
- ✅ 帮助和取消操作 (`/help`, `/cancel`)

### 普通用户权限

普通用户（在 `ALLOWED_USER_IDS` 中配置但不在 `ADMIN_USER_IDS` 中）仅有基础功能权限：

- ✅ 媒体搜索和导入 (`/search`)
- ✅ 自动导入功能 (`/auto`)
- ✅ 帮助和取消操作 (`/help`, `/cancel`)
- ❌ URL 链接导入 (`/url`) - 需要管理员权限
- ❌ 刷新数据源 (`/refresh`) - 需要管理员权限
- ❌ Token 管理 (`/tokens`) - 需要管理员权限
- ❌ 用户权限管理 (`/users`) - 需要管理员权限

### 用户管理功能

管理员可以通过 `/users` 命令动态管理用户权限：

- 📋 查看当前用户列表（管理员和普通用户）
- ➕ 添加新用户到允许列表
- 🗑️ 从允许列表中移除用户
- 🔄 实时刷新用户列表
- 💾 用户权限变更自动保存到配置文件

> 💡 **提示**: 普通用户可以看到所有功能选项，但点击管理员专用功能时会收到权限不足的提示。用户权限变更会立即生效并持久化保存。

## 功能特性

- 🤖 Telegram 机器人集成
- 🔐 用户权限管理（管理员/普通用户分级）
- 👥 动态用户管理（添加/删除用户，实时生效）
- 💾 配置持久化（用户权限、API 配置自动保存）
- 🎯 媒体搜索和导入
- 🔄 自动导入功能
- 📢 支持 emby Webhook 自动入库/刷新
- 🧠 TMDB 智能搜索辅助（自动识别电影/电视剧类型）
- 📺 TVDB 链接解析支持（自动识别 TVDB 链接并获取媒体信息）
- 🎭 豆瓣链接解析支持（自动识别豆瓣链接并获取媒体信息）
- 🌟 IMDB 链接解析支持（自动识别 IMDB 链接并获取媒体信息）
- 🎯 Bangumi(BGM)链接解析支持（自动识别 BGM 链接并获取媒体信息，支持 API 和爬虫双模式）
- 🌐 代理支持
- 📊 详细日志记录
- 🔥 热重载开发支持

## 许可证

MIT License
