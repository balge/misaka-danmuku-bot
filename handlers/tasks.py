from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes, CommandHandler
from telegram.constants import ParseMode # 建议显式导入ParseMode
from utils.permission import check_user_permission
from utils.api import call_danmaku_api
import logging
from datetime import datetime
from typing import List

# 初始化日志
logger = logging.getLogger(__name__)

# 状态映射字典
STATUS_MAPPING = {
    "in_progress": "进行中",
    "completed": "已完成",
}

def get_status_display(status: str) -> str:
    """获取状态的中文显示名称"""
    return STATUS_MAPPING.get(status, status)

def escape_markdown(text: str) -> str:
    """转义Markdown V2的特殊字符"""
    if not isinstance(text, str):
        return ""
    # 这是MarkdownV2需要转义的特殊字符
    special_chars = r'_*[]()~`>#+-=|{}.!'
    for char in special_chars:
        text = text.replace(char, f'\\{char}')
    return text

# ==================== 新增的消息分割函数 ====================
def split_message_by_lines(text: str, chunk_size: int = 4096) -> list[str]:
    """
    按行分割长文本，确保每个分片不超过 Telegram 的最大长度限制。
    这是为了确保消息能被正常发送。
    """
    if len(text) <= chunk_size:
        return [text]

    chunks = []
    current_chunk = ""
    lines = text.split('\n')

    for line in lines:
        # 预检查：如果当前块加上新的一行会超长
        if len(current_chunk) + len(line) + 1 > chunk_size:
            # 如果当前块有内容，先保存
            if current_chunk:
                chunks.append(current_chunk)
            # 重置当前块
            current_chunk = line
        else:
            # 如果当前块是空的，直接赋值
            if not current_chunk:
                current_chunk = line
            # 否则，添加新行
            else:
                current_chunk += '\n' + line

    # 不要忘记最后一个分片
    if current_chunk:
        chunks.append(current_chunk)
        
    return chunks
# ==========================================================

@check_user_permission
async def tasks_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    处理/tasks命令，获取并显示任务列表
    支持status参数过滤，默认显示in_progress状态的任务
    """
    try:
        # 解析命令参数，默认status=in_progress
        args = context.args
        status = "in_progress"  # 默认状态
        
        if args:
            for arg in args:
                if arg.startswith("status="):
                    status = arg.split("=")[1]
                    break
        
        # 调用API获取任务列表
        params = {"status": status}
        api_response = call_danmaku_api("GET", "/tasks", params=params)
        
        if not api_response["success"]:
            await update.message.reply_text(
                f"❌ 获取任务列表失败：{api_response.get('error', '未知错误')}"
            )
            return
        
        tasks_data = api_response["data"]
        
        status_display = get_status_display(status)
        if not tasks_data or len(tasks_data) == 0:
            await update.message.reply_text(
                f"📋 暂无 {status_display} 状态的任务"
            )
            return

        # 格式化任务列表消息
        message = f"📋 *任务列表* \\(状态: {status_display}\\)\n\n"
        
        for i, task in enumerate(tasks_data, 1):
            task_id = task.get("taskId", "未知")
            title = task.get("title", "未知任务")
            progress = task.get("progress", 0)
            description = task.get("description", "")
            created_at = task.get("createdAt", "")
            task_status = task.get("status", "未知")
            
            formatted_time = ""
            if created_at:
                try:
                    dt = datetime.fromisoformat(created_at.replace('Z', '+00:00'))
                    formatted_time = dt.strftime("%Y-%m-%d %H:%M")
                except:
                    formatted_time = created_at
            
            escaped_title = escape_markdown(title)
            task_info = f"*{i}\\. {escaped_title}*\n"
            task_info += f"    🆔 ID: `{task_id}`\n"
            task_info += f"    📊 进度: {progress}%\n"
            
            if task_status != status:
                task_status_display = get_status_display(task_status)
                task_info += f"    🏷️ 状态: {task_status_display}\n"
            
            if description:
                desc_preview = description[:50] + "..." if len(description) > 50 else description
                escaped_desc = escape_markdown(desc_preview)
                task_info += f"    📝 描述: {escaped_desc}\n"
            
            if formatted_time:
                task_info += f"    🕐 创建时间: {escape_markdown(formatted_time)}\n"
            
            message += task_info + "\n"
        
        # 添加操作按钮
        keyboard = [
            [
                InlineKeyboardButton("🔄 刷新", callback_data=f"tasks_refresh_{status}"),
            ],
        ]
        
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        # ==================== 修改后的发送逻辑 ====================
        # 使用分割函数处理可能过长的消息
        message_chunks = split_message_by_lines(message)
        total_chunks = len(message_chunks)

        for i, chunk in enumerate(message_chunks):
            is_last_chunk = (i == total_chunks - 1)
            
            await update.message.reply_text(
                text=chunk,
                parse_mode=ParseMode.MARKDOWN_V2,
                # 只在最后一条消息上附加按钮
                reply_markup=reply_markup if is_last_chunk else None
            )
        # ========================================================
            
        logger.info(f"✅ 用户 {update.effective_user.id} 查看了任务列表 (状态: {status})")
        
    except Exception as e:
        logger.error(f"❌ 处理tasks命令时出错: {e}", exc_info=True)
        await update.message.reply_text(
            "❌ 处理任务列表请求时出现错误，请稍后重试"
        )

def create_tasks_handler():
    """
    创建tasks命令处理器
    """
    return CommandHandler("tasks", tasks_command)
