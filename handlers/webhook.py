import logging
import json
import os
import asyncio
import re
import uuid
from typing import Dict, Any, Optional, List
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from telegram import Bot
from config import ConfigManager
from handlers.import_url import search_video_by_keyword
from utils.tmdb_api import get_tmdb_media_details, search_tv_series_by_name_year, validate_tv_series_match
from utils.api import call_danmaku_api
from utils.security import mask_sensitive_data
from utils.emby_name_converter import convert_emby_series_name
from utils.webhook_filter import should_filter_webhook_title
from utils.rate_limit import should_block_by_rate_limit

logger = logging.getLogger(__name__)

class WebhookHandler:
    """Webhook处理器，用于处理来自Emby等媒体服务器的通知"""
    
    def __init__(self, bot: Optional[Bot] = None):
        self.config = ConfigManager()
        self.bot = bot
        # 从环境变量读取时区配置，默认为Asia/Shanghai
        self.timezone = ZoneInfo(os.getenv('TZ', 'Asia/Shanghai'))
        self._tmdb_cache = {}  # TMDB搜索结果缓存
        self._play_event_cache = {}  # 播放事件缓存，避免重复处理
        
    def validate_api_key(self, provided_key: str) -> bool:
        """验证API密钥"""
        if not self.config.webhook.enabled:
            logger.warning("🔒 Webhook功能未启用，拒绝请求")
            return False
            
        if not provided_key:
            logger.warning("🔒 缺少API密钥")
            return False
            
        if provided_key != self.config.webhook.api_key:
            logger.warning(f"🔒 API密钥验证失败: {mask_sensitive_data(provided_key)}")
            return False
            
        return True
    
    async def handle_emby_webhook(self, data: Dict[str, Any], api_key: str) -> Dict[str, Any]:
        """处理Emby webhook通知
        
        Args:
            data: Emby发送的webhook数据
            api_key: 请求中的API密钥
            
        Returns:
            Dict[str, Any]: 响应数据
        """
        try:
            # 验证API密钥
            if not self.validate_api_key(api_key):
                return {
                    "success": False,
                    "error": "API密钥验证失败",
                    "code": 401
                }
            
            # 解析Emby通知数据
            event_type = data.get('Event', '')
            logger.info(f"📡 收到Emby通知，事件类型: {event_type}")
            
            # 记录完整的Emby消息体到日志（DEBUG级别）
            logger.debug(f"📋 完整Emby消息体:\n{json.dumps(data, indent=2, ensure_ascii=False)}")
            
            # 记录关键信息到INFO级别日志
            item_info = data.get('Item', {})
            session_info = data.get('Session', {})
            user_info = data.get('User', {})
            logger.info(f"🔗 提供商ID: {item_info.get('ProviderIds', {})}")
            
            # 只处理播放开始事件
            if event_type != 'playback.start':
                logger.info(f"ℹ️ 忽略非播放开始事件: {event_type}")
                return {
                    "success": True,
                    "message": f"事件 {event_type} 已忽略",
                    "processed": False
                }
            
            # 提取媒体信息
            media_info = self._extract_media_info(data)
            if not media_info:
                logger.warning("⚠️ 无法提取媒体信息")
                return {
                    "success": False,
                    "error": "无法提取媒体信息",
                    "code": 400
                }
            
            # 记录播放事件
            tmdb_info = f" [TMDB: {media_info['tmdb_id']}]" if media_info.get('tmdb_id') else ""
            logger.info(
                f"🎬 Emby播放开始: {media_info['title']} "
                f"{tmdb_info}"
            )
            
            # 执行智能影视库管理流程
            await self._process_smart_library_management(media_info)
            
            return {
                "success": True,
                "message": "播放开始事件已处理",
                "processed": True,
                "media_info": media_info
            }
            
        except Exception as e:
            logger.error(f"❌ 处理Emby webhook时发生错误: {e}", exc_info=True)
            return {
                "success": False,
                "error": f"处理webhook时发生错误: {str(e)}",
                "code": 500
            }
    
    async def handle_jellyfin_webhook(self, data: Dict[str, Any], api_key: str) -> Dict[str, Any]:
        """处理Jellyfin webhook通知
        
        Args:
            data: Jellyfin发送的webhook数据
            api_key: 请求中的API密钥
            
        Returns:
            Dict[str, Any]: 响应数据
        """
        try:
            # 验证API密钥
            if not self.validate_api_key(api_key):
                return {
                    "success": False,
                    "error": "API密钥验证失败",
                    "code": 401
                }
            
            # 解析Jellyfin通知数据 - 使用Jellyfin标准字段
            event_type = data.get('NotificationType', '')
            logger.info(f"📡 收到Jellyfin通知，事件类型: {event_type}")
            
            # 记录完整的Jellyfin消息体到日志（DEBUG级别）
            logger.debug(f"📋 完整Jellyfin消息体:\n{json.dumps(data, indent=2, ensure_ascii=False)}")
            
            # 记录关键信息到INFO级别日志 - 使用Jellyfin字段结构
            # Jellyfin webhook直接在根级别提供字段，不使用嵌套结构
            logger.info(f"📺 媒体信息: {data.get('Name', '未知')} (类型: {data.get('ItemType', '未知')})")
            logger.info(f"👤 用户信息: {data.get('NotificationUsername', '未知')} | 设备: {data.get('DeviceName', '未知')} ({data.get('ClientName', '未知')})")
            
            # 记录Provider ID信息
            provider_fields = {k: v for k, v in data.items() if k.startswith('Provider_')}
            logger.info(f"🔗 提供商ID: {provider_fields}")
            
            # 只处理播放开始事件
            if event_type != 'PlaybackStart':
                logger.info(f"ℹ️ 忽略非播放开始事件: {event_type}")
                return {
                    "success": True,
                    "message": f"事件 {event_type} 已忽略",
                    "processed": False
                }
            
            # 提取媒体信息
            media_info = self._extract_jellyfin_media_info(data)
            if not media_info:
                logger.warning("⚠️ 无法提取媒体信息")
                return {
                    "success": False,
                    "error": "无法提取媒体信息",
                    "code": 400
                }
            
            # 记录播放事件
            tmdb_info = f" [TMDB: {media_info['tmdb_id']}]" if media_info.get('tmdb_id') else ""
            logger.info(
                f"🎬 Jellyfin播放开始: {media_info['title']} "
                f"{tmdb_info}"
            )
            
            # 执行智能影视库管理流程
            await self._process_smart_library_management(media_info)
            
            return {
                "success": True,
                "message": "播放开始事件已处理",
                "processed": True,
                "media_info": media_info
            }
            
        except Exception as e:
            logger.error(f"❌ 处理Jellyfin webhook时发生错误: {e}", exc_info=True)
            return {
                "success": False,
                "error": f"处理webhook时发生错误: {str(e)}",
                "code": 500
            }
    
    def _extract_jellyfin_media_info(self, data: Dict[str, Any]) -> Optional[Dict[str, str]]:
        """从Jellyfin webhook数据中提取媒体信息
        
        Args:
            data: Jellyfin webhook数据
            
        Returns:
            Optional[Dict[str, str]]: 提取的媒体信息，如果提取失败则返回None
        """
        try:
            # 根据Jellyfin webhook文档，直接从根级别获取字段
            # 参考: https://github.com/jellyfin/jellyfin-plugin-webhook
            
            # 提取基本信息 - 使用Jellyfin标准字段名
            title = data.get('Name', '未知标题')
            media_type = data.get('ItemType', '未知类型')  # Jellyfin使用ItemType而非Type
            year = data.get('Year', '')
            
            # 对于剧集，提取季和集信息 - 使用Jellyfin标准字段
            season_number = data.get('SeasonNumber')  # Jellyfin直接提供SeasonNumber
            episode_number = data.get('EpisodeNumber')  # Jellyfin直接提供EpisodeNumber
            series_name = data.get('SeriesName')  # Jellyfin直接提供SeriesName
            
            # 优化年份提取：优先使用PremiereDate
            if not year and data.get('PremiereDate'):
                try:
                    premiere_date = datetime.fromisoformat(data['PremiereDate'].replace('Z', '+00:00'))
                    year = premiere_date.year
                    logger.debug(f"📅 从PremiereDate提取年份: {year}")
                except Exception as e:
                    logger.debug(f"解析PremiereDate失败: {e}")
            
            # 对于剧集类型，确保数字类型转换
            if season_number is not None:
                try:
                    season_number = int(season_number)
                except (ValueError, TypeError):
                    season_number = None
                    
            if episode_number is not None:
                try:
                    episode_number = int(episode_number)
                except (ValueError, TypeError):
                    episode_number = None
            
            # 清理剧集名称
            if series_name:
                import re
                series_name = series_name.strip()
                # 移除常见的无用后缀
                series_name = re.sub(r'\s*\(\d{4}\)\s*$', '', series_name)  # 移除年份括号
                series_name = re.sub(r'\s*-\s*Season\s+\d+\s*$', '', series_name, flags=re.IGNORECASE)  # 移除季度后缀
            
            # 应用名称转换映射（如果是剧集且有必要信息）
            identify_matched = False  # 标记是否匹配了识别词
            converted_series_name = None
            converted_season_number = None
            converted_title = None  # 电影转换后的标题
            
            if media_type == 'Episode' and series_name and season_number:
                try:
                    converted_result = convert_emby_series_name(series_name, season_number)
                    if converted_result:
                        logger.info(f"🔄 名称转换成功: '{series_name}' S{season_number:02d} -> '{converted_result['series_name']}' S{converted_result['season_number']:02d}")
                        converted_series_name = converted_result['series_name']
                        converted_season_number = converted_result['season_number']
                        identify_matched = True  # 标记匹配了识别词
                    else:
                        logger.debug(f"📝 未找到名称转换规则: '{series_name}' S{season_number:02d}")
                except Exception as e:
                    logger.warning(f"⚠️ 名称转换时发生错误: {e}，使用原始名称")
            elif media_type == 'Movie':
                # 为电影类型添加识别词转换
                try:
                    converted_result = convert_emby_series_name(title, 1)
                    if converted_result:
                        logger.info(f"🎬 电影名称转换成功: '{title}' -> '{converted_result['series_name']}'")
                        converted_title = converted_result['series_name']
                        identify_matched = True  # 标记匹配了识别词
                    else:
                        logger.debug(f"📝 未找到电影名称转换规则: '{title}'")
                except Exception as e:
                    logger.warning(f"⚠️ 电影名称转换时发生错误: {e}，使用原始名称")
            
            # 提取Provider ID信息 - 使用Jellyfin标准字段格式
            # Jellyfin webhook提供Provider_{providerId_lowercase}格式的字段
            tmdb_id = data.get('Provider_tmdb') or data.get('Provider_TheMovieDb')
            imdb_id = data.get('Provider_imdb')
            tvdb_id = data.get('Provider_tvdb') or data.get('Provider_TheTVDB')
            douban_id = data.get('Provider_douban') or data.get('Provider_DoubanMovie')
            bangumi_id = data.get('Provider_bangumi') or data.get('Provider_BGM')
            
            # 调试日志：显示提供商ID信息
            provider_fields = {k: v for k, v in data.items() if k.startswith('Provider_')}
            logger.debug(f"🔍 Jellyfin Provider字段: {provider_fields}")
            logger.debug(f"🎯 提取的Provider ID: TMDB={tmdb_id}, IMDB={imdb_id}, TVDB={tvdb_id}, Douban={douban_id}, Bangumi={bangumi_id}")
            logger.debug(f"🎯 最终提取信息: 剧集='{series_name}', 季度={season_number}, 集数={episode_number}, 年份={year}, TMDB_ID={tmdb_id}")
            
            # 构建完整标题
            if media_type == 'Episode' and series_name:
                if season_number and episode_number:
                    full_title = f"{series_name} S{season_number:02d}E{episode_number:02d} - {title}"
                else:
                    full_title = f"{series_name} - {title}"
            else:
                full_title = f"{title} ({year})" if year else title
            
            return {
                "title": full_title,
                "original_title": title,
                "type": media_type,
                "year": str(year) if year else '',
                "series_name": series_name or '',
                "season": str(season_number) if season_number else '',
                "episode": str(episode_number) if episode_number else '',
                "tmdb_id": tmdb_id or '',
                "imdb_id": imdb_id or '',
                "tvdb_id": tvdb_id or '',
                "douban_id": douban_id or '',
                "bangumi_id": bangumi_id or '',
                "converted_info": {  # 新增converted_info字段
                    "converted_title": converted_title,
                    "converted_series_name": converted_series_name or '',
                    "converted_season_number": str(converted_season_number) if converted_season_number else '',
                    "identify_matched": identify_matched
                },
                "timestamp": datetime.now().isoformat()
            }
            
        except Exception as e:
            logger.error(f"❌ 提取媒体信息时发生错误: {e}")
            return None
    
    def _extract_media_info(self, data: Dict[str, Any]) -> Optional[Dict[str, str]]:
        """从Emby webhook数据中提取媒体信息
        
        Args:
            data: Emby webhook数据
            
        Returns:
            Optional[Dict[str, str]]: 提取的媒体信息，如果提取失败则返回None
        """
        try:
            item = data.get('Item', {})
            session = data.get('Session', {})
            user = data.get('User', {})
            
            # 提取基本信息
            title = item.get('Name', '未知标题')
            media_type = item.get('Type', '未知类型')
            year = item.get('ProductionYear', '')
            
            # 对于剧集，提取季和集信息
            season_number = item.get('ParentIndexNumber')
            episode_number = item.get('IndexNumber')
            series_name = item.get('SeriesName')
            
            # 优化年份提取：优先使用PremiereDate
            if not year and 'PremiereDate' in item and item['PremiereDate']:
                try:
                    premiere_date = datetime.fromisoformat(item['PremiereDate'].replace('Z', '+00:00'))
                    year = premiere_date.year
                    logger.debug(f"📅 从PremiereDate提取年份: {year}")
                except Exception as e:
                    logger.debug(f"解析PremiereDate失败: {e}")
            
            # 优化剧集名称提取：从路径中补充信息
            if not series_name and 'Path' in data:
                path = data['Path']
                import os
                import re
                
                path_parts = [p for p in path.split('/') if p.strip()]
                if len(path_parts) >= 3:
                    # 通常剧集名在倒数第三个或第四个位置
                    for i in range(-4, -1):
                        if abs(i) <= len(path_parts):
                            potential_name = path_parts[i]
                            # 跳过明显的季度文件夹
                            if not re.match(r'^Season\s+\d+$', potential_name, re.IGNORECASE):
                                series_name = potential_name
                                logger.debug(f"📺 从路径提取剧集名: {series_name}")
                                break
                
                # 从文件名中提取季集信息（如果Item中没有）
                if (not season_number or not episode_number) and path:
                    filename = os.path.basename(path)
                    patterns = [
                        r'S(\d+)E(\d+)',  # S01E01
                        r'Season\s*(\d+).*Episode\s*(\d+)',  # Season 1 Episode 1
                        r'第(\d+)季.*第(\d+)集',  # 第1季第1集
                        r'(\d+)x(\d+)',  # 1x01
                    ]
                    
                    for pattern in patterns:
                        match = re.search(pattern, filename, re.IGNORECASE)
                        if match:
                            if not season_number:
                                season_number = int(match.group(1))
                                logger.debug(f"📊 从文件名提取季度: S{season_number}")
                            if not episode_number:
                                episode_number = int(match.group(2))
                                logger.debug(f"📊 从文件名提取集数: E{episode_number}")
                            break
                
                # 从路径中提取年份（如果Item中没有）
                if not year:
                    year_match = re.search(r'\b(19|20)\d{2}\b', path)
                    if year_match:
                        year = int(year_match.group())
                        logger.debug(f"📅 从路径提取年份: {year}")
            
            # 清理剧集名称
            if series_name:
                import re
                series_name = series_name.strip()
                # 移除常见的无用后缀
                series_name = re.sub(r'\s*\(\d{4}\)\s*$', '', series_name)  # 移除年份括号
                series_name = re.sub(r'\s*-\s*Season\s+\d+\s*$', '', series_name, flags=re.IGNORECASE)  # 移除季度后缀
            
            # 应用名称转换映射（如果是剧集且有必要信息）
            identify_matched = False  # 标记是否匹配了识别词
            converted_series_name = None
            converted_season_number = None
            converted_title = None  # 电影转换后的标题
            
            if media_type == 'Episode' and series_name and season_number:
                try:
                    converted_result = convert_emby_series_name(series_name, season_number)
                    if converted_result:
                        logger.info(f"🔄 名称转换成功: '{series_name}' S{season_number:02d} -> '{converted_result['series_name']}' S{converted_result['season_number']:02d}")
                        converted_series_name = converted_result['series_name']
                        converted_season_number = converted_result['season_number']
                        identify_matched = True  # 标记匹配了识别词
                    else:
                        logger.debug(f"📝 未找到名称转换规则: '{series_name}' S{season_number:02d}")
                except Exception as e:
                    logger.warning(f"⚠️ 名称转换时发生错误: {e}，使用原始名称")
            elif media_type == 'Movie':
                # 为电影类型添加识别词转换
                try:
                    converted_result = convert_emby_series_name(title, 1)
                    if converted_result:
                        logger.info(f"🎬 电影名称转换成功: '{title}' -> '{converted_result['series_name']}'")
                        converted_title = converted_result['series_name']
                        identify_matched = True  # 标记匹配了识别词
                    else:
                        logger.debug(f"📝 未找到电影名称转换规则: '{title}'")
                except Exception as e:
                    logger.warning(f"⚠️ 电影名称转换时发生错误: {e}，使用原始名称")
            
            # 提取Provider ID信息（Emby刮削后的元数据）
            provider_ids = item.get('ProviderIds', {})
            tmdb_id = provider_ids.get('Tmdb') or provider_ids.get('TheMovieDb')
            imdb_id = provider_ids.get('Imdb')
            tvdb_id = provider_ids.get('Tvdb') or provider_ids.get('TheTVDB')
            douban_id = provider_ids.get('Douban') or provider_ids.get('DoubanMovie')
            bangumi_id = provider_ids.get('Bangumi') or provider_ids.get('BGM')
            
            # 调试日志：显示提供商ID信息
            logger.debug(f"🔍 媒体提供商ID信息: {provider_ids}")
            logger.debug(f"🎯 提取的Provider ID: TMDB={tmdb_id}, IMDB={imdb_id}, TVDB={tvdb_id}, Douban={douban_id}, Bangumi={bangumi_id}")
            logger.debug(f"🎯 最终提取信息: 剧集='{series_name}', 季度={season_number}, 集数={episode_number}, 年份={year}, TMDB_ID={tmdb_id}")
            
            # 构建完整标题
            if media_type == 'Episode' and series_name:
                if season_number and episode_number:
                    full_title = f"{series_name} S{season_number:02d}E{episode_number:02d} - {title}"
                else:
                    full_title = f"{series_name} - {title}"
            else:
                full_title = f"{title} ({year})" if year else title
            
            return {
                "title": full_title,
                "original_title": title,
                "type": media_type,
                "year": str(year) if year else '',
                "series_name": series_name or '',
                "season": str(season_number) if season_number else '',
                "episode": str(episode_number) if episode_number else '',
                "tmdb_id": tmdb_id or '',
                "imdb_id": imdb_id or '',
                "tvdb_id": tvdb_id or '',
                "douban_id": douban_id or '',
                "bangumi_id": bangumi_id or '',
                "converted_info": {  # 新增converted_info字段
                    "converted_title": converted_title or title,
                    "converted_series_name": converted_series_name or series_name or '',
                    "converted_season_number": str(converted_season_number) if converted_season_number else '',
                    "identify_matched": identify_matched
                },
                "timestamp": datetime.now().isoformat()
            }
            
        except Exception as e:
            logger.error(f"❌ 提取媒体信息时发生错误: {e}")
            return None
    
    async def _process_smart_library_management(self, media_info: Dict[str, Any]):
        """执行智能影视库管理流程
        
        Args:
            media_info: 媒体信息
        """
        try:
            # 使用rate_limit模块检查限流状态
            should_block, _ = should_block_by_rate_limit()
            if should_block:
                return
            
            # 检查是否应该过滤此内容
            title = media_info.get('original_title')
            series_name = media_info.get('series_name')
            
            if should_filter_webhook_title(title, series_name):
                return
            
            # 检查是否为重复播放事件
            if self._is_duplicate_play_event(media_info, cooldown_hours=self.config.webhook.play_event_cooldown_hours):
                return  # 跳过重复处理
            
            # 记录播放事件
            self._record_play_event(media_info)
            
            media_type = media_info.get('type', '')
            title = media_info.get('title')
            
            # 获取优先级Provider信息
            provider_type, provider_id, search_type = self._get_priority_provider_info(media_info)
            
            # 详细检查缺失的信息
            missing_info = []
            if not provider_id:
                missing_info.append('Provider ID')
            if not title:
                missing_info.append('标题')
            
            # 对于电视剧，如果缺少Provider ID但有剧集名称，尝试通过名称搜索TMDB ID
            # 仅记录日志
            if not provider_id and media_type == 'Episode':
                series_name = media_info.get('series_name')
                year = media_info.get('year')
                if series_name:
                    logger.info(f"🔍 电视剧缺少Provider ID，尝试通过剧集名称搜索TMDB ID: {series_name} ({year})")
                    logger.debug(f"📺 剧集信息: 名称='{series_name}', 年份='{year}', 季数='{media_info.get('season')}', 集数='{media_info.get('episode')}'")
            
            # 如果仍然缺少关键信息，跳过智能管理
            if not provider_id and not title:
                logger.info(f"ℹ️ 媒体缺少必要信息（{', '.join(missing_info)}），跳过智能管理")
                logger.debug(f"🔍 媒体信息详情: Provider='{provider_type}:{provider_id}', 标题='{title}', 类型='{media_type}'")
                return
            elif not provider_id:
                logger.info(f"⚠️ 媒体缺少Provider ID但有标题信息，继续处理: {title}")
                logger.debug(f"🔍 媒体信息详情: Provider='{provider_type}:{provider_id}', 标题='{title}', 类型='{media_type}'")
            else:
                logger.info(f"✅ 使用优先级Provider: {provider_type.upper()} ID={provider_id}")
            
            # 更新media_info中的Provider信息
            media_info['selected_provider_type'] = provider_type
            media_info['selected_provider_id'] = provider_id
            media_info['selected_search_type'] = search_type
            
            # 根据媒体类型选择处理方式
            if media_type == 'Movie':
                await self._process_movie_management(media_info)
            elif media_type == 'Episode':
                await self._process_tv_management(media_info)
            else:
                logger.info(f"ℹ️ 不支持的媒体类型: {media_type}，跳过智能管理")
                
        except Exception as e:
            logger.error(f"❌ 智能影视库管理处理失败: {e}", exc_info=True)
    
    async def _process_movie_management(self, media_info: Dict[str, str]):
        """处理电影智能管理流程
        
        Args:
            media_info: 电影媒体信息
        """
        try:
            # 获取优先级 provider 信息
            provider_id = media_info.get('selected_provider_id')
            provider_type = media_info.get('selected_provider_type', 'tmdb')
            
            movie_title = media_info.get('original_title') or media_info.get('title')
            year = media_info.get('year', '')
            
            logger.info(f"🎬 开始电影智能管理: {movie_title} ({year}) ({provider_type.upper()}: {provider_id})")
            
            # 1. 检查库中的电影，使用电影名称进行匹配
            matches = search_video_by_keyword(movie_title, media_type='movie')
            
            # 电影严格匹配策略：优先完全匹配的标题和年份
            exact_matches = [match for match in matches 
                           if match.get('title', '').lower() == movie_title.lower()]
            
            # 如果有年份信息，进一步筛选年份匹配的结果
            if year and exact_matches:
                year_matched = []
                for match in exact_matches:
                    match_year = match.get('year', '')
                    if match_year and str(match_year) == str(year):
                        year_matched.append(match)
                        logger.debug(f"🎯 找到标题和年份完全匹配的电影: {match.get('title')} ({match_year})")
                
                # 如果找到年份匹配的结果，优先使用；否则使用标题匹配的结果
                if year_matched:
                    exact_matches = year_matched
                    logger.info(f"✅ 使用标题+年份匹配结果: {len(year_matched)} 个匹配项")
                else:
                    logger.warning(f"⚠️ 标题匹配但年份不匹配，使用标题匹配结果: {movie_title} (期望年份: {year})")
            elif year:
                logger.debug(f"📅 年份信息可用但无精确标题匹配: {year}")
            
            if not exact_matches:
                # 未找到精确匹配：检查是否为识别词匹配
                converted_info = media_info.get('converted_info', {})
                identify_matched = converted_info.get('identify_matched', False)
                if identify_matched:
                    # 识别词匹配时直接使用关键词导入
                    logger.info(f"🎯 识别词匹配且库中无对应资源，直接使用关键词导入: {movie_title}")
                    await self._import_movie_by_provider(None, 'keyword', movie_title, converted_info)
                elif provider_id:
                    # 非识别词匹配时使用优先级 provider ID 自动导入电影
                    logger.info(f"📥 未找到匹配的电影，开始自动导入: {movie_title} ({year}) 使用 {provider_type.upper()} ID")
                    await self._import_movie_by_provider(provider_id, provider_type, movie_title, converted_info)
                else:
                    logger.warning(f"⚠️ 无法导入电影，缺少有效的 provider ID: {movie_title}")
            else:
                # 存在匹配项：使用refresh功能更新电影数据
                selected_match = exact_matches[0]
                logger.info(f"🔄 找到匹配的电影，开始刷新: {selected_match.get('title', movie_title)}")
                
                # 获取源列表进行刷新
                anime_id = selected_match.get('animeId')
                refresh_success = False
                if anime_id:
                    try:
                        sources_response = call_danmaku_api('GET', f'/library/anime/{anime_id}/sources')
                        if sources_response and sources_response.get('success'):
                            sources = sources_response.get('data', [])
                            if sources:
                                source_id = sources[0].get('sourceId')
                                if source_id:
                                    await self._refresh_movie(source_id, selected_match.get('title', movie_title))
                                    refresh_success = True
                                else:
                                    logger.error(f"❌ 无法获取源ID: {selected_match.get('title')}")
                            else:
                                logger.warning(f"⚠️ 未找到可用源: {selected_match.get('title')}")
                        else:
                            logger.error(f"❌ 获取源列表失败: {selected_match.get('title')}")
                    except Exception as e:
                        logger.warning(f"⚠️ 刷新电影时发生错误，可能资源已被删除: {e}")
                        logger.info(f"💡 由于资源库缓存，将继续执行TMDB智能识别")
                else:
                    logger.error(f"❌ 无法获取动漫ID: {selected_match.get('title')}")
                
                # 如果刷新失败，继续执行TMDB智能识别和导入
                if not refresh_success:
                    converted_info = media_info.get('converted_info', {})
                    await self._fallback_tmdb_search_and_import(movie_title, year, media_type='movie', 
                                                               provider_id=provider_id, provider_type=provider_type, converted_info=converted_info)
                    
        except Exception as e:
            logger.error(f"❌ 电影智能管理处理失败: {e}", exc_info=True)
    
    async def _process_tv_management(self, media_info: Dict[str, str]):
        """处理电视剧智能管理流程
        
        Args:
            media_info: 电视剧媒体信息
        """
        try:
            # 获取优先级 provider 信息
            provider_id = media_info.get('selected_provider_id')
            provider_type = media_info.get('selected_provider_type', 'tmdb')
            
            series_name = media_info.get('series_name') or media_info.get('title')
            season = media_info.get('season')
            episode = media_info.get('episode')
            year = media_info.get('year', '')
            
            if not series_name:
                logger.info("ℹ️ 电视剧缺少剧集名称，跳过智能管理")
                return
            
            # 确保season和episode是整数类型
            try:
                season = int(season) if season else 0
                episode = int(episode) if episode else 0
            except (ValueError, TypeError):
                logger.warning(f"⚠️ 无效的季集编号: season={season}, episode={episode}")
                season = 0
                episode = 0
            
            logger.info(f"🤖 开始电视剧智能管理: {series_name} {'S' + str(season).zfill(2) if season else ''}{('E' + str(episode).zfill(2)) if episode else ''} ({provider_type.upper() if provider_type else 'NONE'}: {provider_id})")
            
            # 使用剧名搜索电视剧类型的内容
            matches = search_video_by_keyword(series_name, 'tv_series')
            logger.info(f"📊 剧名搜索结果: {len(matches)} 个")
            
            # 简化匹配逻辑：标题模糊匹配 + 季度匹配 + 年份匹配
            def is_title_match(title1: str, title2: str) -> bool:
                """检查标题是否模糊匹配"""
                t1, t2 = title1.lower(), title2.lower()
                return t1 == t2 or t1 in t2 or t2 in t1
            
            def is_season_match(season1, season2) -> bool:
                """检查季度是否匹配"""
                if not season1 or not season2:
                    return False
                try:
                    return int(season1) == int(season2)
                except (ValueError, TypeError):
                    return str(season1) in str(season2) or str(season2) in str(season1)
            
            def is_year_match(year1, year2) -> bool:
                """检查年份是否匹配"""
                if not year1 or not year2:
                    return False
                return str(year1) == str(year2)
            
            # 第一轮匹配：标题 + 季度 + 年份
            season_matches = []
            series_name_lower = series_name.lower()
            
            for match in matches:
                match_title = match.get('title', '')
                match_season = match.get('season', '')
                match_year = match.get('year', '')
                
                title_matched = is_title_match(series_name, match_title)
                season_matched = is_season_match(season, match_season)
                year_matched = is_year_match(year, match_year)
                
                # 优先级1：标题 + 季度 + 年份都匹配
                if title_matched and season_matched and year_matched:
                    season_matches.append(match)
                    logger.debug(f"✅ 完全匹配: {match_title} S{match_season} ({match_year})")
            
            # 如果没有完全匹配，降级到标题 + 季度匹配
            if not season_matches:
                for match in matches:
                    match_title = match.get('title', '')
                    match_season = match.get('season', '')
                    
                    title_matched = is_title_match(series_name, match_title)
                    season_matched = is_season_match(season, match_season)
                    
                    if title_matched and season_matched:
                        season_matches.append(match)
                        logger.debug(f"⚠️ 标题+季度匹配: {match_title} S{match_season}")
            
            logger.info(f"📊 Library匹配结果: 找到 {len(season_matches)} 个匹配项")
            if season_matches:
                for i, match in enumerate(season_matches[:10]):  # 只显示前10个
                    logger.info(f"  {i+1}. {match.get('title')} (season={match.get('season')}, year={match.get('year')}, ID: {match.get('animeId')})")
            
            # 检查是否有季度匹配
            exact_season_match = len(season_matches) > 0
            
            # 如果没有找到季度匹配、没有完全匹配的季度或未匹配到具体集数，尝试通过TMDB API搜索
            # 但如果识别词匹配，则跳过TMDB搜索直接使用关键词导入
            converted_info = media_info.get('converted_info', {})
            identify_matched = converted_info.get('identify_matched', False)
            should_search_tmdb = (
                not season_matches or 
                (season and not exact_season_match) or 
                not episode
            ) and not provider_id and not identify_matched
            
            # 如果识别词匹配但库中无对应资源，直接使用关键词导入
            if identify_matched and not season_matches:
                logger.info(f"🎯 识别词匹配且库中无对应资源，直接使用关键词导入: {series_name}")
                await self._import_episodes_by_provider(None, 'keyword', season, [episode, episode + 1] if episode else None, series_name, converted_info)
                return True
            
            if should_search_tmdb:
                logger.info(f"🔍 触发TMDB搜索原因: 无匹配项={not season_matches}, 季度不匹配={season and not exact_season_match}, 无集数={not episode}")
                
                # 先检查缓存
                cached_result = self._get_cached_tmdb_result(series_name)
                tmdb_search_result = None
                
                if cached_result:
                    logger.info(f"💾 使用缓存的TMDB结果: {series_name}")
                    tmdb_search_result = cached_result
                else:
                    logger.info(f"🔍 开始TMDB搜索: {series_name} ({year if year else '年份未知'})")
                    tmdb_search_result = search_tv_series_by_name_year(series_name, year)
                    
                    if tmdb_search_result:
                        # 缓存搜索结果
                        self._cache_tmdb_result(series_name, tmdb_search_result)
                
                if tmdb_search_result:
                    # 增强的匹配验证
                    match_score = self._calculate_match_score(tmdb_search_result, series_name, year, season)
                    logger.info(f"📊 TMDB匹配评分: {tmdb_search_result.get('name')} ({tmdb_search_result.get('year', 'N/A')}) - {match_score}分")
                    
                    if match_score >= 70:  # 设置合理的匹配阈值
                        found_tmdb_id = tmdb_search_result.get('tmdb_id')
                        logger.info(f"✅ TMDB搜索匹配成功: {tmdb_search_result.get('name')} - 匹配分数: {match_score}")
                        logger.info(f"📥 开始自动导入: {series_name} S{season} (TMDB: {found_tmdb_id})")
                        await self._import_episodes_by_provider(found_tmdb_id, 'tmdb', season, [episode, episode + 1] if episode else None, series_name)
                        return True
                    else:
                        logger.info(f"❌ TMDB搜索结果匹配度不足: {tmdb_search_result.get('name')} - 匹配分数: {match_score}")
                else:
                    logger.info(f"❌ TMDB搜索未找到结果: {series_name}")
            
            # 如果通过季度匹配到多个结果，执行严格匹配策略
            final_matches = []
            if season_matches:
                # 严格匹配：完全匹配剧集名称
                for match in season_matches:
                    match_title = match.get('title', '').lower()
                    # 移除季度信息后进行比较
                    clean_match_title = match_title.replace(f'season {season}', '').replace(f's{season}', '')\
                                      .replace(f'第{season}季', '').replace(f'第{season}部', '').strip()
                    clean_series_name = series_name.lower().strip()
                    
                    if clean_match_title == clean_series_name:
                        final_matches.append(match)
                        break  # 找到完全匹配就停止
                
                # 如果没有完全匹配，使用第一个季度匹配结果
                if not final_matches:
                    final_matches = [season_matches[0]]
            else:
                # 如果没有季度匹配，尝试完全匹配
                for match in matches:
                    match_title = match.get('title', '').lower().strip()
                    if match_title == series_name.lower().strip():
                        final_matches.append(match)
                        break
            
            if not final_matches:
                # 未找到匹配项：检查是否有 provider ID 进行自动导入
                if provider_id:
                    logger.info(f"📥 未找到匹配项，开始自动导入: {series_name} S{season} ({provider_type.upper()}: {provider_id})")
                    converted_info = media_info.get('converted_info', {})
                    identify_matched = converted_info.get('identify_matched', False)
                    await self._import_episodes_by_provider(provider_id, provider_type, season, [episode, episode + 1] if episode else None, series_name, converted_info)
                else:
                    # 尝试通过TMDB API搜索获取TMDB ID
                    logger.info(f"🔍 未找到匹配项且缺少 provider ID，尝试通过TMDB搜索: {series_name} ({year})")
                    tmdb_search_result = search_tv_series_by_name_year(series_name, year)
                    
                    if tmdb_search_result:
                        # 验证搜索结果是否匹配
                        if validate_tv_series_match(tmdb_search_result, series_name, year, season, episode):
                            found_tmdb_id = tmdb_search_result.get('tmdb_id')
                            logger.info(f"✅ TMDB搜索成功，找到匹配的剧集: {tmdb_search_result.get('name')} (ID: {found_tmdb_id})")
                            logger.info(f"📥 开始自动导入: {series_name} S{season} (TMDB: {found_tmdb_id})")
                            converted_info = media_info.get('converted_info', {})
                            identify_matched = converted_info.get('identify_matched', False)
                            await self._import_episodes_by_provider(found_tmdb_id, 'tmdb', season, [episode, episode + 1] if episode else None, series_name, converted_info)
                        else:
                            logger.warning(f"⚠️ TMDB搜索结果验证失败: {series_name}")
                            logger.debug(f"💡 建议: 请检查剧集名称和年份是否正确，或在Emby中添加正确的TMDB刮削信息")
                    else:
                        logger.info(f"ℹ️ TMDB搜索未找到匹配结果: {series_name} ({year})")
                        logger.debug(f"💡 建议: 请检查剧集名称和年份是否正确，或在Emby中添加TMDB刮削信息")
            else:
                # 存在匹配项：使用refresh功能更新
                selected_match = final_matches[0]
                logger.info(f"🔄 找到匹配项，开始刷新: {selected_match.get('title', series_name)} S{season}")
                
                # 获取源列表进行刷新
                anime_id = selected_match.get('animeId')
                refresh_success = False
                if anime_id:
                    try:
                        sources_response = call_danmaku_api('GET', f'/library/anime/{anime_id}/sources')
                        if sources_response and sources_response.get('success'):
                            sources = sources_response.get('data', [])
                            if sources:
                                source_id = sources[0].get('sourceId')
                                if source_id:
                                    # 传递剧集名称和年份，用于TMDB搜索
                                    converted_info = media_info.get('converted_info', {})
                                    await self._refresh_episodes(source_id, [episode, episode + 1], provider_id, provider_type, season, series_name, year, converted_info)
                                    refresh_success = True
                                else:
                                    logger.error(f"❌ 无法获取源ID: {selected_match.get('title')}")
                            else:
                                logger.warning(f"⚠️ 未找到可用源: {selected_match.get('title')}")
                        else:
                            logger.error(f"❌ 获取源列表失败: {selected_match.get('title')}")
                    except Exception as e:
                        logger.warning(f"⚠️ 刷新剧集时发生错误，可能资源已被删除: {e}")
                        logger.info(f"💡 由于资源库缓存，将继续执行TMDB智能识别")
                else:
                    logger.error(f"❌ 无法获取资源ID: {selected_match.get('title')}")
                
                # 如果刷新失败，继续执行TMDB智能识别
                if not refresh_success:
                    converted_info = media_info.get('converted_info', {})
                    identify_matched = converted_info.get('identify_matched', False)
                    await self._fallback_tmdb_search_and_import(series_name, year, season, episode, 'tv',
                                                               provider_id=provider_id, provider_type=provider_type, converted_info=converted_info)
                    
        except Exception as e:
            logger.error(f"❌ 电视剧智能管理处理失败: {e}", exc_info=True)
    
    async def _fallback_tmdb_search_and_import(self, title: str, year: str = None, season: int = None, episode: int = None, 
                                             media_type: str = 'tv', provider_id: str = None, provider_type: str = None, converted_info: Dict[str, Any] = None):
        """TMDB辅助查询和导入的通用方法
        
        Args:
            title: 媒体标题
            year: 年份
            season: 季度（仅电视剧）
            episode: 集数（仅电视剧）
            media_type: 媒体类型 ('tv' 或 'movie')
            provider_id: 优先级provider ID
            provider_type: 优先级provider类型
            converted_info: 转换信息字典
        """
        try:
            # 从converted_info中提取identify_matched
            identify_matched = converted_info.get('identify_matched', False) if converted_info else False
            
            # 如果识别词匹配，直接使用关键词导入，跳过TMDB搜索
            if identify_matched:
                logger.info(f"🎯 识别词匹配，直接使用关键词导入: {title}")
                if media_type == 'movie':
                    await self._import_movie_by_provider(None, 'keyword', title, converted_info)
                    return
                elif media_type == 'tv':
                    await self._import_episodes_by_provider(None, 'keyword', season, [episode, episode + 1] if episode else None, title, converted_info)
                    return
            
            # 优先使用provider信息进行导入
            if provider_id and provider_type:
                logger.info(f"📥 使用优先级provider进行导入: {title} ({provider_type.upper()}: {provider_id})")
                
                if media_type == 'movie':
                    await self._import_movie_by_provider(provider_id, provider_type, title, converted_info)
                    return
                elif media_type == 'tv':
                    await self._import_episodes_by_provider(provider_id, provider_type, season, None, title, converted_info)
                    return
            
            if media_type == 'movie':
                logger.info(f"🔍 刷新失败，开始TMDB智能识别: {title} ({year})")
                
                # 触发TMDB搜索逻辑
                cached_result = self._get_cached_tmdb_result(title)
                tmdb_search_result = None
                
                if cached_result:
                    logger.info(f"💾 使用缓存的TMDB结果: {title}")
                    tmdb_search_result = cached_result
                else:
                    logger.info(f"🔍 开始TMDB搜索: {title} ({year if year else '年份未知'})")
                    from utils.tmdb_api import search_movie_by_name_year
                    tmdb_search_result = search_movie_by_name_year(title, year)
                    
                    if tmdb_search_result:
                        # 缓存搜索结果
                        self._cache_tmdb_result(title, tmdb_search_result)
                
                if tmdb_search_result:
                    # 增强的匹配验证
                    match_score = self._calculate_movie_match_score(tmdb_search_result, title, year)
                    logger.info(f"📊 TMDB电影匹配评分: {tmdb_search_result.get('title')} ({tmdb_search_result.get('year', 'N/A')}) - {match_score}分")
                    
                    if match_score >= 70:  # 设置合理的匹配阈值
                        found_tmdb_id = tmdb_search_result.get('tmdb_id')
                        logger.info(f"✅ TMDB电影搜索匹配成功: {tmdb_search_result.get('title')} - 匹配分数: {match_score}")
                        
                        # 使用TMDB ID导入电影
                        await self._import_movie_by_tmdb_id(found_tmdb_id)
                        return
                    else:
                        logger.info(f"⚠️ TMDB电影匹配分数过低({match_score}分)，尝试fallback方案")
                
                # TMDB搜索失败或匹配分数过低
                logger.warning(f"⚠️ TMDB搜索失败，跳过导入: {title}")
                return
            
            if media_type == 'tv':
                logger.info(f"🔍 刷新失败，开始TMDB智能识别: {title} ({year})")
                
                # 触发TMDB搜索逻辑
                cached_result = self._get_cached_tmdb_result(title)
                tmdb_search_result = None
                
                if cached_result:
                    logger.info(f"💾 使用缓存的TMDB结果: {title}")
                    tmdb_search_result = cached_result
                else:
                    logger.info(f"🔍 开始TMDB搜索: {title} ({year if year else '年份未知'})")
                    tmdb_search_result = search_tv_series_by_name_year(title, year)
                    
                    if tmdb_search_result:
                        # 缓存搜索结果
                        self._cache_tmdb_result(title, tmdb_search_result)
                
                if tmdb_search_result:
                    # 增强的匹配验证
                    match_score = self._calculate_match_score(tmdb_search_result, title, year, season)
                    logger.info(f"📊 TMDB匹配评分: {tmdb_search_result.get('name')} ({tmdb_search_result.get('year', 'N/A')}) - {match_score}分")
                    
                    if match_score >= 70:  # 设置合理的匹配阈值
                        found_tmdb_id = tmdb_search_result.get('tmdb_id')
                        logger.info(f"✅ TMDB搜索匹配成功: {tmdb_search_result.get('name')} - 匹配分数: {match_score}")
                        logger.info(f"📥 开始自动导入: {title} S{season} (TMDB: {found_tmdb_id})")
                        await self._import_episodes_by_provider(found_tmdb_id, 'tmdb', season, [episode, episode + 1] if episode else None, title)
                    else:
                        logger.warning(f"⚠️ TMDB搜索结果验证失败: {title}")
                else:
                    logger.info(f"ℹ️ TMDB搜索未找到匹配结果: {title} ({year})")
                    
        except Exception as e:
            logger.error(f"❌ TMDB辅助查询处理失败: {e}", exc_info=True)
    
    def _calculate_movie_match_score(self, tmdb_result: dict, movie_title: str, year: str = None) -> int:
        """计算电影TMDB匹配评分
        
        Args:
            tmdb_result: TMDB搜索结果
            movie_title: 原始电影标题
            year: 年份（可选）
            
        Returns:
            匹配评分 (0-100)
        """
        if not tmdb_result:
            return 0
        
        score = 0
        tmdb_title = tmdb_result.get('title', '')
        tmdb_original_title = tmdb_result.get('original_title', '')
        tmdb_year = tmdb_result.get('year', '')
        
        # 标题匹配评分 (最高60分)
        movie_title_lower = movie_title.lower().strip()
        tmdb_title_lower = tmdb_title.lower().strip()
        tmdb_original_title_lower = tmdb_original_title.lower().strip()
        
        if movie_title_lower == tmdb_title_lower or movie_title_lower == tmdb_original_title_lower:
            score += 60  # 完全匹配
        elif movie_title_lower in tmdb_title_lower or tmdb_title_lower in movie_title_lower:
            score += 40  # 包含匹配
        elif movie_title_lower in tmdb_original_title_lower or tmdb_original_title_lower in movie_title_lower:
            score += 35  # 原标题包含匹配
        else:
            # 计算字符串相似度
            similarity = self._calculate_string_similarity(movie_title_lower, tmdb_title_lower)
            score += int(similarity * 30)  # 相似度匹配，最高30分
        
        # 年份匹配评分 (最高30分)
        if year and tmdb_year:
            try:
                year_diff = abs(int(year) - int(tmdb_year))
                if year_diff == 0:
                    score += 30  # 年份完全匹配
                elif year_diff == 1:
                    score += 20  # 年份相差1年
                elif year_diff <= 2:
                    score += 10  # 年份相差2年内
                # 年份相差超过2年不加分
            except (ValueError, TypeError):
                pass
        elif not year:
            score += 15  # 没有年份信息，给予中等分数
        
        # 受欢迎度加分 (最高10分)
        popularity = tmdb_result.get('popularity', 0)
        if popularity > 50:
            score += 10
        elif popularity > 20:
            score += 5
        elif popularity > 5:
            score += 2
        
        return min(score, 100)  # 确保不超过100分
    
    def _calculate_match_score(self, tmdb_result: dict, series_name: str, year: Optional[str], season: Optional[int]) -> int:
        """计算TMDB搜索结果的匹配分数
        
        Args:
            tmdb_result: TMDB搜索结果
            series_name: 剧集名称
            year: 年份
            season: 季度
            
        Returns:
            匹配分数 (0-200)
        """
        import time
        
        score = 0
        tmdb_name = tmdb_result.get('name', '').lower()
        tmdb_original_name = tmdb_result.get('original_name', '').lower()
        series_name_lower = series_name.lower()
        
        # 名称匹配评分 (最高100分)
        if series_name_lower == tmdb_name or series_name_lower == tmdb_original_name:
            score += 100  # 完全匹配
        elif series_name_lower in tmdb_name or series_name_lower in tmdb_original_name:
            score += 70   # 包含匹配
        elif tmdb_name in series_name_lower or tmdb_original_name in series_name_lower:
            score += 50   # 被包含匹配
        
        # 年份匹配评分 (最高30分)
        if year and tmdb_result.get('year'):
            tmdb_year = int(tmdb_result.get('year'))
            input_year = int(year)
            if tmdb_year == input_year:
                score += 30  # 年份完全匹配
            elif abs(tmdb_year - input_year) <= 1:
                score += 15  # 年份相差1年
        
        # 季度验证评分 (最高20分)
        if season and tmdb_result.get('number_of_seasons'):
            number_of_seasons = tmdb_result.get('number_of_seasons', 0)
            if number_of_seasons >= season:
                score += 20  # 季度数量合理
        
        return score
    
    def _cache_tmdb_result(self, series_name: str, tmdb_result: dict) -> None:
        """缓存TMDB搜索结果
        
        Args:
            series_name: 剧集名称
            tmdb_result: TMDB搜索结果
        """
        import time
        
        cache_key = series_name.lower().strip()
        self._tmdb_cache[cache_key] = {
            'result': tmdb_result,
            'timestamp': time.time()
        }
        logger.debug(f"💾 缓存TMDB搜索结果: {series_name} -> {tmdb_result.get('name')}")
    
    def _get_cached_tmdb_result(self, series_name: str) -> Optional[dict]:
        """获取缓存的TMDB搜索结果
        
        Args:
            series_name: 剧集名称
            
        Returns:
            缓存的TMDB结果，如果不存在或过期则返回None
        """
        import time
        
        cache_key = series_name.lower().strip()
        cached = self._tmdb_cache.get(cache_key)
        
        if cached:
            # 检查缓存是否过期 (24小时)
            if time.time() - cached['timestamp'] < 86400:
                logger.debug(f"💾 使用缓存的TMDB结果: {series_name}")
                return cached['result']
            else:
                # 清理过期缓存
                del self._tmdb_cache[cache_key]
                logger.debug(f"🗑️ 清理过期TMDB缓存: {series_name}")
        
        return None
    
    def _generate_media_key(self, media_info: Dict[str, str]) -> str:
        """生成媒体唯一标识符
        
        Args:
            media_info: 媒体信息
            
        Returns:
            str: 媒体唯一标识符
        """
        # 优先使用Provider ID作为唯一标识
        provider_ids = []
        for provider in ['tmdb_id', 'imdb_id', 'tvdb_id', 'douban_id', 'bangumi_id']:
            if media_info.get(provider):
                provider_ids.append(f"{provider}:{media_info[provider]}")
        
        if provider_ids:
            base_key = "|".join(provider_ids)
        else:
            # 如果没有Provider ID，使用标题和年份
            title = media_info.get('title', '').lower().strip()
            year = media_info.get('year', '')
            base_key = f"title:{title}|year:{year}"
        
        # 对于电视剧，添加季度和集数信息
        if media_info.get('type') == 'Episode':
            season = media_info.get('season', '')
            episode = media_info.get('episode', '')
            base_key += f"|season:{season}|episode:{episode}"
        
        return base_key
    
    def _is_duplicate_play_event(self, media_info: Dict[str, str], cooldown_hours: Optional[int] = None) -> bool:
        """检查是否为重复的播放事件
        
        Args:
            media_info: 媒体信息
            cooldown_hours: 冷却时间（小时），默认1小时
            
        Returns:
            bool: 如果是重复事件返回True
        """
        import time
        
        # 使用传入的冷却时间或配置文件中的默认值
        if cooldown_hours is None:
            cooldown_hours = self.config.webhook.play_event_cooldown_hours
        
        media_key = self._generate_media_key(media_info)
        current_time = time.time()
        cooldown_seconds = cooldown_hours * 3600
        
        # 检查缓存中是否存在该媒体的最近播放记录
        if media_key in self._play_event_cache:
            last_play_time = self._play_event_cache[media_key]
            if current_time - last_play_time < cooldown_seconds:
                logger.info(f"⏰ 检测到重复播放事件，跳过处理: {media_info.get('title')} (冷却中，剩余 {int((cooldown_seconds - (current_time - last_play_time)) / 60)} 分钟)")
                return True
        
        return False
    
    def _record_play_event(self, media_info: Dict[str, str]) -> None:
        """记录播放事件
        
        Args:
            media_info: 媒体信息
        """
        import time
        
        media_key = self._generate_media_key(media_info)
        current_time = time.time()
        
        # 记录播放时间
        self._play_event_cache[media_key] = current_time
        
        # 清理过期的缓存记录（超过24小时）
        expired_keys = []
        for key, timestamp in self._play_event_cache.items():
            if current_time - timestamp > 86400:  # 24小时
                expired_keys.append(key)
        
        for key in expired_keys:
            del self._play_event_cache[key]
        
        logger.debug(f"📝 记录播放事件: {media_info.get('title')} (缓存大小: {len(self._play_event_cache)})")
    
    async def _import_movie_by_tmdb_id(self, tmdb_id: str):
        """使用TMDB ID导入电影
        
        Args:
            tmdb_id: TMDB电影ID
        """
        try:
            logger.info(f"📥 开始导入电影 (TMDB: {tmdb_id})")
            
            # 调用导入API
            import_params = {
                "searchType": "tmdb",
                "searchTerm": tmdb_id,
                "mediaType": "movie",
                "originalKeyword": f"TMDB ID: {tmdb_id}"  # 添加原始关键词用于识别词匹配
            }
            
            response = call_danmaku_api('POST', '/import/auto', params=import_params)
            
            # 添加详细的API响应日志
            logger.info(f"🔍 电影导入 /import/auto API响应: {response}")
            
            if response and response.get('success'):
                # 从data字段中获取taskId
                data = response.get('data', {})
                task_id = data.get('taskId')
                logger.info(f"📊 响应data字段: {data}")
                logger.info(f"✅ 电影导入成功 (TMDB: {tmdb_id}), taskId: {task_id}")
                
                # 导入成功后刷新library缓存
                # 库缓存刷新已移除，改为直接调用/library/search接口
                logger.info("✅ 电影导入成功")
            else:
                error_msg = response.get('message', '未知错误') if response else '请求失败'
                logger.error(f"❌ 电影导入失败 (TMDB: {tmdb_id}): {error_msg}")
                
        except Exception as e:
            logger.error(f"❌ 导入电影时发生错误 (TMDB: {tmdb_id}): {e}", exc_info=True)
    
    async def _import_movie_by_provider(self, provider_id: str, provider_type: str = 'tmdb', movie_title: str = None, converted_info: Dict[str, Any] = None):
        """使用优先级 provider 导入单个电影
        
        Args:
            provider_id: Provider ID (tmdb_id, tvdb_id, imdb_id, douban_id, 或 bangumi_id)
            provider_type: Provider 类型 ('tmdb', 'tvdb', 'imdb', 'douban', 'bangumi')
            movie_title: 电影标题
            converted_info: 转换信息字典
        """
        try:
            logger.info(f"📥 开始导入电影 ({provider_type.upper()}: {provider_id})")
            
            # 标记是否使用关键字模式
            use_keyword_mode = False
            
            # 如果是TMDB provider，尝试获取TMDB详情
            if provider_type.lower() == 'tmdb':
                try:
                    tmdb_details = get_tmdb_media_details(provider_id, 'movie')
                    if not tmdb_details:
                        logger.warning(f"⚠️ TMDB详情获取失败: {provider_id}")
                        use_keyword_mode = True
                    else:
                        logger.info(f"✅ TMDB详情获取成功: {tmdb_details.get('title', 'Unknown')}")
                except Exception as e:
                    logger.error(f"❌ TMDB详情获取异常: {provider_id} - {e}")
                    use_keyword_mode = True
            
            # 构建导入参数
            converted_info = converted_info or {}
            identify_matched = converted_info.get('identify_matched', False)
            converted_title = converted_info.get('converted_title', movie_title)
            
            if identify_matched and converted_title:
                # 识别词匹配时使用关键词模式，优先使用转换后的标题
                import_params = {
                    "searchType": "keyword",
                    "searchTerm": converted_title,
                    "originalKeyword": movie_title,
                    "mediaType": "movie"
                }
                logger.info(f"🎯 使用关键词模式导入电影 (识别词匹配): {movie_title} -> {converted_title}")
            elif use_keyword_mode and movie_title:
                # TMDB详情获取失败时自动切换至关键字模式
                import_params = {
                    "searchType": "keyword",
                    "searchTerm": movie_title,
                    "originalKeyword": movie_title,
                    "mediaType": "movie"
                }
                logger.info(f"🔄 TMDB详情获取失败，自动切换至关键字模式: {movie_title}")
            elif use_keyword_mode and not movie_title:
                # TMDB详情获取失败且无电影标题，跳过导入
                logger.warning(f"❌ TMDB详情获取失败且无movie_title，跳过导入: {provider_id}")
                return
            else:
                # 默认使用provider模式
                import_params = {
                    "searchType": provider_type,
                    "searchTerm": provider_id,
                    "mediaType": "movie",
                    "originalKeyword": movie_title if movie_title else f"{provider_type.upper()} ID: {provider_id}"
                }
                logger.info(f"🚀 使用Provider模式导入电影: {provider_type.upper()} {provider_id}")
            
            response = call_danmaku_api('POST', '/import/auto', params=import_params)
            
            # 构建媒体信息用于回调通知
            media_info = {
                'Name': movie_title if movie_title else f"{provider_type.upper()} {provider_id}",
                'Type': 'Movie',
                'ProviderId': provider_id,
                'ProviderType': provider_type
            }
            
            
            if response and response.get('success'):
                # 从data字段中获取taskId
                data = response.get('data', {})
                task_id = data.get('taskId')
                logger.info(f"✅ 电影导入成功 ({provider_type.upper()}: {provider_id}), taskId: {task_id}")
                
                # 发送成功回调通知，传递taskId
                if task_id:
                    # 使用webhook专用task_polling_manager
                    from utils.task_polling import webhook_task_polling_manager
                    await webhook_task_polling_manager.send_callback_notification('import', media_info, 'success', task_ids=[task_id])
                else:
                    # 使用webhook专用task_polling_manager
                    from utils.task_polling import webhook_task_polling_manager
                    await webhook_task_polling_manager.send_callback_notification('import', media_info, 'success')
                
                # 库缓存刷新已移除，改为直接调用/library/search接口
            else:
                error_msg = response.get('message', '未知错误') if response else '请求失败'
                logger.error(f"❌ 电影导入失败 ({provider_type.upper()}: {provider_id}): {error_msg}")
                
                # 发送失败回调通知
                # 使用webhook专用task_polling_manager
                from utils.task_polling import webhook_task_polling_manager
                await webhook_task_polling_manager.send_callback_notification('import', media_info, 'failed', error_msg)
                
        except Exception as e:
            logger.error(f"❌ 导入电影时发生错误 ({provider_type.upper()}: {provider_id}): {e}", exc_info=True)
    
    async def _refresh_movie(self, source_id: str, movie_title: str = None):
        """刷新电影数据
        
        Args:
            source_id: 源ID
            movie_title: 电影标题（可选）
        """
        try:
            logger.info(f"🔄 开始刷新电影 (源ID: {source_id})")
            
            # 先获取源的分集列表来获取episodeId
            episodes_response = call_danmaku_api('GET', f'/library/source/{source_id}/episodes')
            if not episodes_response or not episodes_response.get('success'):
                logger.error(f"❌ 获取电影分集列表失败 (源ID: {source_id})")
                return
            
            source_episodes = episodes_response.get('data', [])
            if not source_episodes:
                logger.warning(f"⚠️ 电影源暂无分集信息 (源ID: {source_id})")
                return
            
            # 电影默认只取第一个分集的ID去刷新
            first_episode = source_episodes[0]
            episode_id = first_episode.get('episodeId')
            fetched_at = first_episode.get('fetchedAt')
            
            if not episode_id:
                logger.error(f"❌ 未找到电影的episodeId (源ID: {source_id})")
                return
            
            # 检查时间段判断机制：入库时间是否早于24小时
            if fetched_at:
                try:
                    # 解析fetchedAt时间（ISO 8601格式）并转换为配置的时区
                    fetched_time = datetime.fromisoformat(fetched_at.replace('Z', '+00:00'))
                    fetched_time_local = fetched_time.astimezone(self.timezone)
                    current_time_local = datetime.now(self.timezone)
                    time_diff = current_time_local - fetched_time_local
                    
                    if time_diff < timedelta(hours=24):
                        logger.info(f"⏰ 电影入库时间在24小时内 ({time_diff}），跳过刷新 (源ID: {source_id}) [时区: {self.timezone}]")
                        return
                    else:
                        logger.info(f"⏰ 电影入库时间超过24小时 ({time_diff}），执行刷新 (源ID: {source_id}) [时区: {self.timezone}]")
                except Exception as e:
                    logger.warning(f"⚠️ 解析入库时间失败，继续执行刷新: {e}")
            else:
                logger.info(f"ℹ️ 未找到入库时间信息，继续执行刷新 (源ID: {source_id})")
            
            logger.info(f"🔄 刷新电影分集 (episodeId: {episode_id})")
            
            # 使用episodeId刷新电影
            response = call_danmaku_api(
                method="POST",
                endpoint=f"/library/episode/{episode_id}/refresh"
            )
            
            # 添加调试日志查看完整响应
            logger.info(f"🔍 电影刷新API响应: {response}")
            
            # 构建媒体信息用于回调通知
            media_info = {
                'Name': movie_title if movie_title else f"源ID {source_id}",
                'Type': 'Movie',
                'SourceId': source_id,
                'EpisodeId': episode_id
            }
            
            # 添加电影标题（如果有）
            if movie_title:
                media_info['MovieTitle'] = movie_title
            
            if response and response.get('success'):
                # 从data字段中获取taskId
                data = response.get('data', {})
                task_id = data.get('taskId')
                logger.info(f"📊 响应data字段: {data}")
                logger.info(f"✅ 电影刷新成功 (源ID: {source_id}), taskId: {task_id}")
                
                # 发送成功回调通知，如果有taskId则启动轮询
                if task_id:
                    # 使用webhook专用task_polling_manager
                    from utils.task_polling import webhook_task_polling_manager
                    await webhook_task_polling_manager.send_callback_notification('refresh', media_info, 'success', task_ids=[task_id])
                else:
                    # 使用webhook专用task_polling_manager
                    from utils.task_polling import webhook_task_polling_manager
                    await webhook_task_polling_manager.send_callback_notification('refresh', media_info, 'success')
            else:
                error_msg = response.get('message', '未知错误') if response else '请求失败'
                logger.error(f"❌ 电影刷新失败 (源ID: {source_id}): {error_msg}")
                
                # 发送失败回调通知
                # 使用webhook专用task_polling_manager
                from utils.task_polling import webhook_task_polling_manager
                await webhook_task_polling_manager.send_callback_notification('refresh', media_info, 'failed', error_msg)
                
        except Exception as e:
            logger.error(f"❌ 刷新电影时发生错误 (源ID: {source_id}): {e}", exc_info=True)
    
    async def _import_episodes_by_provider(self, provider_id: str, provider_type: str, season: int, episodes: list, series_name: str = None, converted_info: Dict[str, Any] = None):
        """根据provider类型导入指定集数
        
        Args:
            provider_id: Provider ID (TMDB/TVDB/IMDB/Douban/Bangumi)
            provider_type: Provider类型 ('tmdb', 'tvdb', 'imdb', 'douban', 'bangumi')
            season: 季度
            episodes: 集数列表
            series_name: 剧集名称
            converted_info: 转换信息字典
        """
        if not episodes:
            logger.warning(f"⚠️ 集数列表为空，跳过导入: {provider_type.upper()} {provider_id} S{season}")
            return
        
        # 从converted_info中提取identify_matched和converted_series_name
        identify_matched = converted_info.get('identify_matched', False) if converted_info else False
        converted_series_name = converted_info.get('converted_series_name') if converted_info else None
        converted_season_number = converted_info.get('converted_season_number') if converted_info else None
        
        # 识别词匹配时优先使用转换后的标题
        effective_series_name = converted_series_name if (identify_matched and converted_series_name) else series_name
        effective_season_number = converted_season_number if (identify_matched and converted_season_number) else season
        
        # 根据provider类型设置搜索参数
        search_type_map = {
            'tmdb': 'tmdb',
            'tvdb': 'tvdb', 
            'imdb': 'imdb',
            'douban': 'douban',
            'bangumi': 'bangumi',
            'keyword': 'keyword'
        }
        
        search_type = search_type_map.get(provider_type.lower(), 'tmdb')
        
        # 获取详细信息进行验证（仅TMDB支持）
        max_episodes = 0
        use_keyword_mode = False  # 标记是否需要使用关键词模式
        
        try:
            if provider_type.lower() == 'tmdb':
                tmdb_info = get_tmdb_media_details(provider_id, 'tv_series')
                if tmdb_info:
                    logger.info(f"📺 准备导入剧集: {tmdb_info.get('name', 'Unknown')} ({tmdb_info.get('year', 'N/A')})")
                    
                    # 验证季度有效性
                    seasons = tmdb_info.get('seasons', [])
                    valid_season = None
                    for s in seasons:
                        if s.get('season_number') == season:
                            valid_season = s
                            break
                    
                    if not valid_season:
                        logger.error(f"❌ 无效的季度: S{season}，可用季度: {[s.get('season_number') for s in seasons]}")
                        return
                    
                    max_episodes = valid_season.get('episode_count', 0)
                    logger.info(f"📊 季度信息: S{season} 共{max_episodes}集")
                else:
                    logger.warning(f"⚠️ 无法获取TMDB详细信息: {provider_id}，自动切换至关键字模式")
                    use_keyword_mode = True
            else:
                logger.info(f"📺 准备导入剧集: {provider_type.upper()} {provider_id} S{season}")
        except Exception as e:
            logger.warning(f"⚠️ 验证{provider_type.upper()}信息时出错: {e}，自动切换至关键字模式")
            if provider_type.lower() == 'tmdb':
                use_keyword_mode = True
        
        success_count = 0
        failed_count = 0
        task_ids = []  # 收集成功导入的taskId
        
        try:
            for episode in episodes:
                if episode is None:
                    continue
                    
                # 确保episode是整数类型
                try:
                    episode_num = int(episode) if isinstance(episode, str) else episode
                    if episode_num <= 0:
                        logger.warning(f"⚠️ 跳过无效集数: {episode_num}")
                        continue
                except (ValueError, TypeError):
                    logger.warning(f"⚠️ 跳过无效集数格式: {episode}")
                    continue
                
                # 验证集数是否超出范围（仅TMDB支持）
                if provider_type.lower() == 'tmdb' and max_episodes > 0 and episode_num > max_episodes:
                    logger.warning(f"⚠️ 集数超出范围: S{season}E{episode_num} > {max_episodes}集，跳过")
                    continue
                
                # 构建导入参数
                if identify_matched:
                    # 识别词匹配时使用关键词模式，优先使用转换后的标题
                    import_params = {
                        "searchType": "keyword",
                        "searchTerm": effective_series_name,
                        "mediaType": "tv_series",
                        "season": effective_season_number,
                        "episode": episode_num,
                        "originalKeyword": effective_series_name
                    }
                    logger.info(f"🎯 使用关键词模式导入(识别词匹配): {effective_series_name} S{season:02d}E{episode_num:02d}")
                elif use_keyword_mode and series_name:
                    # TMDB详情获取失败时自动切换至关键词模式
                    import_params = {
                        "searchType": "keyword",
                        "searchTerm": series_name,
                        "mediaType": "tv_series",
                        "season": season,
                        "episode": episode_num,
                        "originalKeyword": series_name
                    }
                    logger.info(f"🔄 自动切换关键词模式导入: {series_name} S{season:02d}E{episode_num:02d}")
                elif use_keyword_mode and not series_name:
                    # TMDB详情获取失败且无series_name时跳过
                    logger.warning(f"❌ 无法获取TMDB详情且无series_name，跳过导入: {provider_id} S{season:02d}E{episode_num:02d}")
                    failed_count += 1
                    continue
                else:
                    # 使用provider模式
                    import_params = {
                        "searchType": search_type,
                        "searchTerm": provider_id,
                        "mediaType": "tv_series",
                        "season": season,
                        "episode": episode_num,
                        "originalKeyword": series_name if series_name else f"{provider_type.upper()} ID: {provider_id}"
                    }
                    logger.info(f"🚀 使用Provider模式导入: {provider_type.upper()} {provider_id} S{season:02d}E{episode_num:02d}")
                
                # 调用导入API
                try:
                    response = call_danmaku_api(
                        method="POST",
                        endpoint="/import/auto",
                        params=import_params
                    )
                    
                    # 添加详细的API响应日志
                    logger.info(f"🔍 /import/auto API响应: {response}")
                    
                    if response and response.get("success"):
                        success_count += 1
                        # 从data字段中获取taskId
                        data = response.get('data', {})
                        task_id = data.get('taskId')
                        logger.info(f"📊 响应data字段: {data}")
                        if task_id:
                            task_ids.append(task_id)
                        logger.info(f"✅ 导入成功: S{season:02d}E{episode_num:02d}, taskId: {task_id}")
                    else:
                        failed_count += 1
                        error_msg = response.get('message', '未知错误') if response else '请求失败'
                        logger.warning(f"⚠️ 导入失败: S{season:02d}E{episode_num:02d} - {error_msg}")
                        
                except Exception as api_error:
                    failed_count += 1
                    logger.error(f"❌ 导入API调用异常: S{season:02d}E{episode_num:02d} - {api_error}")
            
            # 输出导入统计
            total_episodes = success_count + failed_count
            if total_episodes > 0:
                logger.info(f"📊 导入完成: 成功 {success_count}/{total_episodes} 集")
                if failed_count > 0:
                    logger.warning(f"⚠️ {failed_count} 集导入失败，请检查日志")
                
                # 构建媒体信息用于回调通知
                media_info = {
                    'Name': series_name if series_name else f"{provider_type.upper()} {provider_id} S{season}",
                    'Type': 'Series',
                    'ProviderId': provider_id,
                    'ProviderType': provider_type,
                    'Season': season,
                    'Episodes': episodes,
                    'SuccessCount': success_count,
                    'FailedCount': failed_count,
                    'TotalCount': total_episodes
                }
                
                # 添加剧集名称（如果有）
                if series_name:
                    media_info['SeriesName'] = series_name
                
                # 发送回调通知
            if success_count > 0 and failed_count == 0:
                # 全部成功
                # 使用webhook专用task_polling_manager
                from utils.task_polling import webhook_task_polling_manager
                await webhook_task_polling_manager.send_callback_notification('import', media_info, 'success', task_ids=task_ids)
            elif success_count > 0 and failed_count > 0:
                # 部分成功
                # 使用webhook专用task_polling_manager
                from utils.task_polling import webhook_task_polling_manager
                await webhook_task_polling_manager.send_callback_notification('import', media_info, 'success', f"{failed_count} 集导入失败", task_ids=task_ids)
            else:
                # 全部失败
                # 使用webhook专用task_polling_manager
                from utils.task_polling import webhook_task_polling_manager
                await webhook_task_polling_manager.send_callback_notification('import', media_info, 'failed', "所有集数导入失败")
                
                # 库缓存刷新已移除，改为直接调用/library/search接口
                if success_count > 0:
                    logger.info("✅ 集数导入完成")
                    
        except Exception as e:
            logger.error(f"❌ 导入集数异常: {e}", exc_info=True)
    
     
    def _get_priority_provider_info(self, media_info: Dict[str, Any]) -> tuple:
        """
        获取优先级Provider信息 (tmdb > tvdb > imdb > douban > bangumi)
        
        Args:
            media_info: 已提取的媒体信息（包含provider ID）
            
        Returns:
            tuple: (provider_type, provider_id, search_type)
        """
        # 按优先级检查：tmdb > tvdb > imdb > douban > bangumi
        tmdb_id = media_info.get('tmdb_id')
        if tmdb_id:
            return 'tmdb', tmdb_id, 'tmdb'
            
        # 暂时取消tvdb
        # tvdb_id = media_info.get('tvdb_id')
        # if tvdb_id:
        #     return 'tvdb', tvdb_id, 'tvdb'
            
        # imdb_id = media_info.get('imdb_id')
        # if imdb_id:
        #     return 'imdb', imdb_id, 'imdb'
            
        # douban_id = media_info.get('douban_id')
        # if douban_id:
        #     return 'douban', douban_id, 'douban'
            
        # bangumi_id = media_info.get('bangumi_id')
        # if bangumi_id:
        #     return 'bangumi', bangumi_id, 'bangumi'
            
        return None, None, None
    
    async def _refresh_episodes(self, source_id: str, episodes: list, provider_id: Optional[str], provider_type: Optional[str], season_num: int, series_name: Optional[str] = None, year: Optional[str] = None, converted_info: Dict[str, Any] = None):
        """刷新指定集数
        
        Args:
            source_id: 源ID
            episodes: 集数列表
            provider_id: Provider ID（可选，为None时尝试通过TMDB搜索获取）
            provider_type: Provider类型（tmdb, tvdb等，默认为tmdb）
            season_num: 季度号
            series_name: 剧集名称（用于TMDB搜索）
            year: 年份（用于TMDB搜索）
            converted_info: 转换信息字典
        """
        # 从converted_info中提取identify_matched
        identify_matched = converted_info.get('identify_matched', False) if converted_info else False
        try:
            # 先获取源的分集列表来获取episodeId
            episodes_response = call_danmaku_api('GET', f'/library/source/{source_id}/episodes')
            if not episodes_response or not episodes_response.get('success'):
                logger.error(f"❌ 获取分集列表失败: source_id={source_id}")
                return
            
            source_episodes = episodes_response.get('data', [])
            if not source_episodes:
                logger.warning(f"⚠️ 源暂无分集信息: source_id={source_id}")
                return
            
            # 创建集数索引到集信息的映射（包含episodeId和fetchedAt）
            episode_map = {}
            for ep in source_episodes:
                if ep.get('episodeId'):
                    episode_map[ep.get('episodeIndex')] = {
                        'episodeId': ep.get('episodeId'),
                        'fetchedAt': ep.get('fetchedAt')
                    }
            
            success_count = 0
            failed_count = 0
            skipped_count = 0
            task_ids = []  # 收集刷新操作的taskId
            
            # 收集需要导入的集数信息，以便批量处理
            episodes_to_import = []

            for episode in episodes:
                episode_info = episode_map.get(episode)
                if not episode_info:
                    # 当集数不存在时，根据识别词匹配状态决定处理方式
                    if identify_matched:
                        logger.info(f"🔍 未找到第{episode}集且识别词匹配，直接关键词导入第{episode}集: {series_name} S{season_num}E{episode:02d}")
                        await self._import_episodes_by_provider(None, 'keyword', season_num, [episode], series_name, converted_info)
                    else:
                        # 非识别词匹配时，使用provider信息进行导入判断
                        current_provider_id = provider_id
                        current_provider_type = provider_type or 'tmdb'
                        
                        # 如果没有provider ID，尝试通过剧集名称搜索获取
                        if not current_provider_id  and series_name:
                            logger.info(f"🔍 未找到第{episode}集且缺少Provider ID，尝试通过TMDB搜索: {series_name} ({year})")
                            tmdb_search_result = search_tv_series_by_name_year(series_name, year)
                            
                            if tmdb_search_result:
                                # 验证搜索结果是否匹配
                                if validate_tv_series_match(tmdb_search_result, series_name, year, season_num, episode):
                                    current_provider_id = tmdb_search_result.get('tmdb_id')
                                    logger.info(f"✅ TMDB搜索成功，找到匹配的剧集: {tmdb_search_result.get('name')} (ID: {current_provider_id})")
                                else:
                                    logger.warning(f"⚠️ TMDB搜索结果验证失败: {series_name}")
                            else:
                                logger.info(f"ℹ️ TMDB搜索未找到匹配结果: {series_name} ({year})")
                        
                        if current_provider_id:
                            logger.warning(f"⚠️ 未找到第{episode}集的episodeId，收集到导入列表")
                            
                            # 如果是TMDB类型，先尝试获取详情验证
                            final_provider_type = current_provider_type
                            final_series_name = series_name
                            
                            if current_provider_type == 'tmdb':
                                try:
                                    tmdb_info = get_tmdb_media_details(current_provider_id, 'tv_series')
                                    if tmdb_info and tmdb_info.get('name'):
                                        final_series_name = tmdb_info.get('name')
                                        logger.info(f"✅ TMDB详情获取成功: {final_series_name}")
                                    else:
                                        # TMDB详情获取失败，切换到keyword模式
                                        final_provider_type = 'keyword'
                                        logger.warning(f"⚠️ TMDB详情获取失败，切换到keyword模式")
                                except Exception as e:
                                    # TMDB详情获取异常，切换到keyword模式
                                    final_provider_type = 'keyword'
                                    logger.warning(f"⚠️ TMDB详情获取异常: {e}，切换到keyword模式")
                            
                            # 收集需要导入的集数信息，包含series_name
                            episodes_to_import.append((current_provider_id, final_provider_type, season_num, episode, final_series_name))
                        else:
                            logger.info(f"ℹ️ 未找到第{episode}集的episodeId且无法获取Provider ID，跳过导入")
                    continue
                
                episode_id = episode_info['episodeId']
                fetched_at = episode_info['fetchedAt']
                
                # 检查时间段判断机制：入库时间是否早于24小时
                if fetched_at:
                    try:
                        # 解析fetchedAt时间（ISO 8601格式）并转换为配置的时区
                        fetched_time = datetime.fromisoformat(fetched_at.replace('Z', '+00:00'))
                        fetched_time_local = fetched_time.astimezone(self.timezone)
                        current_time_local = datetime.now(self.timezone)
                        time_diff = current_time_local - fetched_time_local
                        
                        if time_diff < timedelta(hours=24):
                            logger.info(f"⏰ 第{episode}集入库时间在24小时内 ({time_diff}），跳过刷新 [时区: {self.timezone}]")
                            skipped_count += 1
                            continue
                        else:
                            logger.info(f"⏰ 第{episode}集入库时间超过24小时 ({time_diff}），执行刷新 [时区: {self.timezone}]")
                    except Exception as e:
                        logger.warning(f"⚠️ 解析第{episode}集入库时间失败，继续执行刷新: {e}")
                else:
                    logger.info(f"ℹ️ 第{episode}集未找到入库时间信息，继续执行刷新")
                
                logger.info(f"🔄 刷新集数: E{episode:02d} (episodeId: {episode_id})")
                
                # 使用新的API端点刷新指定集数
                response = call_danmaku_api(
                    method="POST",
                    endpoint=f"/library/episode/{episode_id}/refresh"
                )
                
                if response and response.get("success"):
                    # 从data字段中获取taskId
                    data = response.get('data', {})
                    task_id = data.get('taskId')
                    if task_id:
                        task_ids.append(task_id)
                        logger.info(f"✅ 集数刷新成功: E{episode:02d}, taskId: {task_id}")
                    else:
                        logger.info(f"✅ 集数刷新成功: E{episode:02d}")
                    success_count += 1
                else:
                    logger.warning(f"⚠️ 集数刷新失败: E{episode:02d}")
                    failed_count += 1
            
            # 构建媒体信息用于回调通知
            total_episodes = len(episodes)
            processed_episodes = success_count + failed_count
            
            if processed_episodes > 0:
                media_info = {
                    'Name': series_name if series_name else f"源ID {source_id} S{season_num}",
                    'Type': 'Series',
                    'SourceId': source_id,
                    'Season': season_num,
                    'Episodes': episodes,
                    'SuccessCount': success_count,
                    'FailedCount': failed_count,
                    'SkippedCount': skipped_count,
                    'TotalCount': total_episodes
                }
                
                # 添加剧集名称和Provider ID（如果有）
                if series_name:
                    media_info['SeriesName'] = series_name
                if provider_id and provider_type == 'tmdb':
                    media_info['TmdbId'] = provider_id
                if year:
                    media_info['Year'] = year
                
                # 发送回调通知
            if success_count > 0 and failed_count == 0:
                # 全部成功
                if task_ids:
                    # 使用webhook专用task_polling_manager
                    from utils.task_polling import webhook_task_polling_manager
                    await webhook_task_polling_manager.send_callback_notification('refresh', media_info, 'success', task_ids=task_ids)
                else:
                    # 使用webhook专用task_polling_manager
                    from utils.task_polling import webhook_task_polling_manager
                    await webhook_task_polling_manager.send_callback_notification('refresh', media_info, 'success')
            elif success_count > 0 and failed_count > 0:
                # 部分成功
                if task_ids:
                    # 使用webhook专用task_polling_manager
                    from utils.task_polling import webhook_task_polling_manager
                    await webhook_task_polling_manager.send_callback_notification('refresh', media_info, 'success', f"{failed_count} 集刷新失败", task_ids=task_ids)
                else:
                    # 使用webhook专用task_polling_manager
                    from utils.task_polling import webhook_task_polling_manager
                    await webhook_task_polling_manager.send_callback_notification('refresh', media_info, 'success', f"{failed_count} 集刷新失败")
            elif failed_count > 0:
                # 全部失败
                # 使用webhook专用task_polling_manager
                from utils.task_polling import webhook_task_polling_manager
                await webhook_task_polling_manager.send_callback_notification('refresh', media_info, 'failed', "所有集数刷新失败", task_ids=task_ids)
            
            # 如果有需要导入的集数，批量处理并发送合并后的通知
            if episodes_to_import:
                await self._import_multiple_episodes(episodes_to_import, series_name)
                
                logger.info(f"📊 刷新完成: 成功 {success_count}/{processed_episodes} 集，跳过 {skipped_count} 集")
                    
        except Exception as e:
            logger.error(f"❌ 刷新集数异常: {e}")
    
    async def _import_multiple_episodes(self, episodes_to_import: list, series_name: Optional[str] = None):
        """批量导入多个集数，并发送合并后的通知
        
        Args:
            episodes_to_import: 需要导入的集数列表，每个元素是(provider_id, provider_type, season_num, episode, series_name)的元组
            series_name: 剧集名称（用于通知，已废弃，使用episodes_to_import中的series_name）
        """
        try:
            total_success = 0
            total_failed = 0
            all_task_ids = []
            imported_episodes = []
            success_episodes = []
            failed_episodes = []
            
            # 批量处理每个需要导入的集数
            for provider_id, provider_type, season_num, episode, episode_series_name in episodes_to_import:
                try:
                    # 根据provider_type构建导入参数
                    search_type_map = {
                        'tmdb': 'tmdb',
                        'tvdb': 'tvdb', 
                        'imdb': 'imdb',
                        'douban': 'douban',
                        'bangumi': 'bangumi',
                        'keyword': 'keyword'
                    }
                    
                    search_type = search_type_map.get(provider_type, 'tmdb')
                    
                    # 根据provider_type确定searchTerm
                    if provider_type == 'keyword':
                        search_term = episode_series_name or str(provider_id)
                    else:
                        search_term = str(provider_id)
                    
                    import_params = {
                        "searchType": search_type,
                        "searchTerm": search_term,
                        "mediaType": "tv_series",
                        "importMethod": "auto",
                        "season": season_num,
                        "episode": episode,
                        "originalKeyword": f"{provider_type.upper()} ID: {provider_id}"  # 添加原始关键词用于识别词匹配
                    }
                    
                    logger.info(f"🚀 开始导入单集: {provider_type.upper()} {provider_id} S{season_num:02d}E{episode:02d}")
                    
                    # 调用导入API
                    response = call_danmaku_api(
                        method="POST",
                        endpoint="/import/auto",
                        params=import_params
                    )
                    
                    imported_episodes.append((provider_id, provider_type, season_num, episode, episode_series_name))
                    
                    if response and response.get("success"):
                        logger.info(f"✅ 单集导入成功: S{season_num:02d}E{episode:02d}")
                        total_success += 1
                        success_episodes.append((season_num, episode))
                        # 从data字段中获取taskId
                        data = response.get('data', {})
                        task_id = data.get('taskId')
                        if task_id:
                            all_task_ids.append(task_id)
                    else:
                        logger.info(f"ℹ️ 单集可能不存在或已导入: S{season_num:02d}E{episode:02d}")
                        total_failed += 1
                        failed_episodes.append((season_num, episode))
                except Exception as e:
                    logger.error(f"❌ 导入单集异常 S{season_num:02d}E{episode:02d}: {e}")
                    total_failed += 1
                    failed_episodes.append((season_num, episode))
            
            # 如果有导入的集数，发送合并后的通知
            if imported_episodes:
                # 获取第一个集数的信息用于通知（假设所有集数属于同一剧集）
                first_provider_id, first_provider_type, first_season, _, first_series_name = imported_episodes[0]
                
                # 使用episodes_to_import中的series_name作为显示名称
                display_name = first_series_name or f"{first_provider_type.upper()} {first_provider_id}"
                
                # 构建媒体信息用于回调通知
                media_info = {
                    'Name': display_name,
                    'Type': 'Series',
                    'ProviderId': first_provider_id,
                    'ProviderType': first_provider_type,
                    'Season': first_season,
                    'Episodes': [ep for _, _, _, ep, _ in imported_episodes],
                    'SuccessCount': total_success,
                    'FailedCount': total_failed,
                    'TotalCount': len(imported_episodes)
                }
                
                # 添加剧集名称（如果有）
                if series_name:
                    media_info['SeriesName'] = series_name
                elif first_series_name:
                    media_info['SeriesName'] = first_series_name
                
                # 构建详细的状态消息
                # 处理task_ids参数
                task_ids_param = None
                if 'all_task_ids' in locals():
                    task_ids_param = all_task_ids
                
                if total_success > 0 and total_failed == 0:
                    # 全部成功
                    # 使用webhook专用task_polling_manager
                    from utils.task_polling import webhook_task_polling_manager
                    await webhook_task_polling_manager.send_callback_notification('import', media_info, 'success', task_ids=task_ids_param)
                elif total_success > 0 and total_failed > 0:
                    # 部分成功
                    # 使用webhook专用task_polling_manager
                    from utils.task_polling import webhook_task_polling_manager
                    await webhook_task_polling_manager.send_callback_notification('import', media_info, 'success', f"{total_failed} 集导入失败", task_ids=task_ids_param)
                else:
                    # 全部失败
                    # 使用webhook专用task_polling_manager
                    from utils.task_polling import webhook_task_polling_manager
                    await webhook_task_polling_manager.send_callback_notification('import', media_info, 'failed', f"所有集数导入失败", task_ids=task_ids_param)
        except Exception as e:
            logger.error(f"❌ 批量导入集数异常: {e}")


# 全局webhook处理器实例
webhook_handler = WebhookHandler()


def set_bot_instance(bot: Bot):
    """设置Bot实例
    
    Args:
        bot: Telegram Bot实例
    """
    global webhook_handler
    webhook_handler.bot = bot
    
    # 同时设置webhook专用task_polling_manager的bot实例
    from utils.task_polling import webhook_task_polling_manager
    webhook_task_polling_manager.bot = bot
    
    logger.info("🔌 Webhook handler and webhook task polling manager bot instances set")