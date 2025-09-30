# 确保从这里开始完整复制
import json
import re
import time
from datetime import datetime, timedelta
from typing import Any, List, Dict, Tuple, Optional

import pytz
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from app.core.config import settings
from app.log import logger
from app.plugins import _PluginBase
from app.schemas import NotificationType
from app.utils.http import RequestUtils


class InvitesFunSignin(_PluginBase):
    # 插件名称
    plugin_name = "药丸签到"
    # 插件描述
    plugin_desc = "药丸(invites.fun)论坛签到。"
    # 插件图标
    plugin_icon = "https://invites.fun/assets/favicon-light-c226e3f7.png"
    # 插件版本
    plugin_version = "1.8.6" # 版本号增加，调整徽章显示位置
    # 插件作者
    plugin_author = "madrays & Gemini"
    # 作者主页
    author_url = "https://github.com/madrays"
    # 插件配置项ID前缀
    plugin_config_prefix = "invitesfunsignin_"
    # 加载顺序
    plugin_order = 25
    # 可使用的用户级别
    auth_level = 2

    # 私有属性
    _enabled = False
    # 任务执行间隔
    _cron = None
    _cookie = None
    _onlyonce = False
    _notify = False
    _history_days = None
    # 重试相关
    _retry_count = 0  # 最大重试次数
    _current_retry = 0  # 当前重试次数
    _retry_interval = 2  # 重试间隔(小时)
    # 代理相关
    _use_proxy = True  # 是否使用代理，默认启用

    # 定时器
    _scheduler: Optional[BackgroundScheduler] = None

    def init_plugin(self, config: dict = None):
        """
        插件初始化
        """
        # 接收参数
        if config:
            self._enabled = config.get("enabled", False)
            self._notify = config.get("notify", False)
            self._cron = config.get("cron", "30 8 * * *")
            self._onlyonce = config.get("onlyonce", False)
            self._cookie = config.get("cookie", "")
            self._history_days = config.get("history_days", 30)
            self._retry_count = int(config.get("retry_count", 0))
            self._retry_interval = int(config.get("retry_interval", 2))
            self._use_proxy = config.get("use_proxy", True)

        # 重置重试计数
        self._current_retry = 0
        self.save_data("current_retry", 0)

        # 停止现有任务
        self.stop_service()

        # 确保scheduler是新的
        self._scheduler = BackgroundScheduler(timezone=settings.TZ)

        # 立即运行一次
        if self._onlyonce:
            logger.info(f"药丸签到服务启动，立即运行一次")
            self._scheduler.add_job(func=self.__signin, trigger='date',
                                    run_date=datetime.now(tz=pytz.timezone(settings.TZ)) + timedelta(seconds=3),
                                    name="药丸签到")
            # 关闭一次性开关
            self._onlyonce = False
            self.update_config({
                "onlyonce": False,
                "cron": self._cron,
                "enabled": self._enabled,
                "notify": self._notify,
                "cookie": self._cookie,
                "history_days": self._history_days,
                "retry_count": self._retry_count,
                "retry_interval": self._retry_interval,
                "use_proxy": self._use_proxy
            })
        # 周期运行
        elif self._cron and self._enabled:
            logger.info(f"药丸签到服务启动，周期：{self._cron}")
            self._scheduler.add_job(func=self.__signin,
                                    trigger=CronTrigger.from_crontab(self._cron),
                                    name="药丸签到")

        # 启动任务
        if self._scheduler.get_jobs():
            self._scheduler.print_jobs()
            self._scheduler.start()

    def _send_notification(self, title, text):
        """
        发送通知
        """
        if self._notify:
            self.post_message(
                mtype=NotificationType.SiteMessage,
                title=title,
                text=text
            )

    def _schedule_retry(self, hours=None):
        """
        安排重试任务
        :param hours: 重试间隔小时数，如果不指定则使用配置的_retry_interval
        """
        if not self._scheduler:
            self._scheduler = BackgroundScheduler(timezone=settings.TZ)

        # 计算下次重试时间
        retry_interval = hours if hours is not None else self._retry_interval
        next_run_time = datetime.now(tz=pytz.timezone(settings.TZ)) + timedelta(hours=retry_interval)
        
        self._current_retry = self.get_data("current_retry") or 0
        self._current_retry += 1
        self.save_data("current_retry", self._current_retry)

        # 安排重试任务
        self._scheduler.add_job(
            func=self.__signin,
            trigger='date',
            run_date=next_run_time,
            name=f"药丸签到重试 ({self._current_retry}/{self._retry_count})"
        )

        logger.info(f"药丸签到失败，将在{retry_interval}小时后重试，当前重试次数: {self._current_retry}/{self._retry_count}")

        # 启动定时器（如果未启动）
        if not self._scheduler.running:
            self._scheduler.start()

    def _send_signin_failure_notification(self, reason: str, attempt: int):
        """
        发送签到失败的通知
        :param reason: 失败原因
        :param attempt: 当前尝试次数
        """
        if self._notify:
            # 检查是否还有后续的定时重试
            current_retry = self.get_data("current_retry") or 0
            remaining_retries = self._retry_count - current_retry
            retry_info = ""
            if self._retry_count > 0 and remaining_retries > 0:
                next_retry_hours = self._retry_interval
                retry_info = (
                    f"🔄 重试信息\n"
                    f"• 将在 {next_retry_hours} 小时后进行下一次定时重试\n"
                    f"• 剩余定时重试次数: {remaining_retries}\n"
                    f"━━━━━━━━━━\n"
                )

            self._send_notification(
                title="【❌ 药丸签到失败】",
                text=(
                    f"📢 执行结果\n"
                    f"━━━━━━━━━━\n"
                    f"🕐 时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
                    f"❌ 状态：签到失败 (已完成 {attempt + 1} 次快速重试)\n"
                    f"💬 原因：{reason}\n"
                    f"━━━━━━━━━━\n"
                    f"{retry_info}"
                )
            )

    def _get_proxies(self):
        """
        获取代理配置
        """
        if not self._use_proxy:
            logger.info("未启用代理")
            return None

        try:
            # 获取系统代理配置
            if hasattr(settings, 'PROXY') and settings.PROXY:
                logger.info(f"使用系统代理: {settings.PROXY}")
                return settings.PROXY
            else:
                logger.warning("系统代理未配置")
                return None
        except Exception as e:
            logger.error(f"获取代理配置出错: {str(e)}")
            return None

    def __signin(self, retry_count=0, max_retries=3):
        """
        药丸签到
        """
        # 增加任务锁，防止重复执行
        if hasattr(self, '_signing_in') and self._signing_in:
            logger.info("已有签到任务在执行，跳过当前任务")
            return

        self._signing_in = True
        attempt = 0
        try:
            # 检查Cookie是否配置
            if not self._cookie:
                logger.error("未配置Cookie，无法进行签到")
                if self._notify:
                    self._send_notification(
                        title="【❌ 药丸签到失败】",
                        text=(
                            f"📢 执行结果\n"
                            f"━━━━━━━━━━\n"
                            f"🕐 时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
                            f"❌ 状态：签到失败，未配置Cookie\n"
                            f"━━━━━━━━━━\n"
                            f"💡 配置方法\n"
                            f"• 在插件配置中填写药丸论坛的 flarum_remember Cookie\n"
                            f"━━━━━━━━━━"
                        )
                    )
                return False

            # 使用循环而非递归实现重试
            for attempt in range(max_retries + 1):
                if attempt > 0:
                    logger.info(f"正在进行第 {attempt}/{max_retries} 次快速重试...")
                    time.sleep(3)  # 重试前等待3秒

                # 获取代理配置
                proxies = self._get_proxies()
                req = RequestUtils(proxies=proxies, timeout=30)

                # 步骤1: 访问主页获取CSRF Token和Session
                logger.info("步骤1: 访问主页获取CSRF Token和Session...")
                initial_cookies_dict = {"flarum_remember": self._cookie}
                try:
                    res_main = req.get_res(url="https://invites.fun/", cookies=initial_cookies_dict)
                except Exception as e:
                    logger.error(f"请求药丸主页出错: {str(e)}")
                    if attempt < max_retries:
                        continue
                    raise Exception("连接站点出错")

                if not res_main or res_main.status_code != 200:
                    logger.error(f"请求药丸主页返回错误状态码: {res_main.status_code if res_main else '无响应'}")
                    if attempt < max_retries:
                        continue
                    raise Exception("无法连接到站点")

                # 提取CSRF Token
                csrfToken_match = re.search(r'"csrfToken":"(.*?)"', res_main.text)
                if not csrfToken_match:
                    logger.error("提取csrfToken失败")
                    if attempt < max_retries:
                        continue
                    raise Exception("无法获取CSRF令牌")
                csrfToken = csrfToken_match.group(1)
                logger.info(f"获取csrfToken成功: {csrfToken}")

                # 提取UserID
                userId_match = re.search(r'"userId":(\d+)', res_main.text)
                if not userId_match or userId_match.group(1) == "0":
                    logger.error("提取userId失败或Cookie无效")
                    if attempt < max_retries:
                        continue
                    raise Exception("无法获取用户ID，请检查Cookie是否有效")
                userId = userId_match.group(1)
                logger.info(f"获取userId成功: {userId}")

                # 提取Session Cookie
                session_cookie = res_main.cookies.get('flarum_session')
                if not session_cookie:
                    logger.error("提取session_cookie失败")
                    if attempt < max_retries:
                        continue
                    raise Exception("无法获取Session，签到流程中断")
                logger.info("获取flarum_session成功")

                # 步骤2: 执行签到
                logger.info("步骤2: 执行签到...")
                final_cookie_str = f"flarum_remember={self._cookie}; flarum_session={session_cookie}"

                headers = {
                    "X-Csrf-Token": csrfToken,
                    "X-Http-Method-Override": "PATCH",
                    "Cookie": final_cookie_str,
                    "Content-Type": "application/json; charset=UTF-8"
                }

                data = {
                    "data": {
                        "type": "users",
                        "attributes": {
                            "canCheckin": False
                        },
                        "id": userId
                    }
                }

                try:
                    res = req.post_res(
                        url=f"https://invites.fun/api/users/{userId}",
                        json=data,
                        headers=headers
                    )
                except Exception as e:
                    logger.error(f"签到请求出错: {str(e)}")
                    if attempt < max_retries:
                        continue
                    raise Exception("签到请求异常")

                if not res or res.status_code != 200:
                    logger.error(f"药丸签到失败，状态码: {res.status_code if res else '无响应'}")
                    if attempt < max_retries:
                        continue
                    raise Exception("API请求错误")

                # 签到成功
                sign_dict = json.loads(res.text)

                self.save_data("user_info", sign_dict)
                logger.info("成功获取并保存用户信息。")

                money = sign_dict['data']['attributes']['money']
                totalContinuousCheckIn = sign_dict['data']['attributes']['totalContinuousCheckIn']
                lastCheckinMoney = sign_dict['data']['attributes'].get('lastCheckinMoney', 0)

                if "canCheckin" in sign_dict['data']['attributes'] and not sign_dict['data']['attributes']['canCheckin']:
                    status_text = "已签到"
                    reward_text = "今日已领取奖励"
                    logger.info(f"药丸已签到，当前药丸: {money}，累计签到: {totalContinuousCheckIn}")
                else:
                    status_text = "签到成功"
                    reward_text = f"获得{lastCheckinMoney}药丸奖励"
                    logger.info(
                        f"药丸签到成功，获得{lastCheckinMoney}药丸，当前药丸: {money}，累计签到: {totalContinuousCheckIn}")

                if self._notify:
                    self._send_notification(
                        title=f"【✅ 药丸{status_text}】",
                        text=(
                            f"📢 执行结果\n"
                            f"━━━━━━━━━━\n"
                            f"🕐 时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
                            f"✨ 状态：{status_text}\n"
                            f"🎁 奖励：{reward_text}\n"
                            f"━━━━━━━━━━\n"
                            f"📊 积分统计\n"
                            f"💊 药丸：{money}\n"
                            f"📆 签到天数：{totalContinuousCheckIn}\n"
                            f"━━━━━━━━━━"
                        )
                    )
                
                current_retry_count = self.get_data("current_retry") or 0
                history = {
                    "date": datetime.today().strftime('%Y-%m-%d %H:%M:%S'),
                    "status": status_text,
                    "money": money,
                    "totalContinuousCheckIn": totalContinuousCheckIn,
                    "lastCheckinMoney": lastCheckinMoney,
                    "fail_count": current_retry_count,
                    "retry": {
                        "enabled": self._retry_count > 0,
                        "current": current_retry_count,
                        "max": self._retry_count
                    }
                }

                self._save_history(history)

                # 签到成功后，重置间隔重试计数
                self.save_data("current_retry", 0)
                logger.info(f"药丸签到成功，重置间隔重试计数器。")

                return True

        except Exception as e:
            logger.error(f"签到过程发生异常: {str(e)}")
            import traceback
            logger.error(f"错误详情: {traceback.format_exc()}")

            self._send_signin_failure_notification(str(e), attempt)
            
            current_retry_count = self.get_data("current_retry") or 0
            if self._retry_count > 0 and current_retry_count < self._retry_count:
                logger.info(f"安排第{current_retry_count + 1}次间隔重试")
                self._schedule_retry()
            else:
                if self._retry_count > 0:
                    logger.info("已达到最大间隔重试次数，不再重试。重置计数器。")
                self.save_data("current_retry", 0)

            return False
        finally:
            self._signing_in = False

    def _save_history(self, record):
        """
        保存签到历史记录
        """
        history = self.get_data('history') or []

        if "失败" in record.get("status", ""):
            record["retry"] = {
                "enabled": self._retry_count > 0,
                "current": self._current_retry,
                "max": self._retry_count
            }

        history.append(record)

        if self._history_days:
            try:
                days_ago = time.time() - int(self._history_days) * 24 * 60 * 60
                history = [r for r in history if
                           datetime.strptime(r["date"], '%Y-%m-%d %H:%M:%S').timestamp() >= days_ago]
            except Exception as e:
                logger.error(f"清理历史记录异常: {str(e)}")

        self.save_data(key="history", value=history)

    def get_state(self) -> bool:
        return self._enabled

    @staticmethod
    def get_command() -> List[Dict[str, Any]]:
        pass

    def get_api(self) -> List[Dict[str, Any]]:
        pass

    def get_service(self) -> List[Dict[str, Any]]:
        services = []
        if self._enabled and self._cron:
            services.append({
                "id": "InvitesFunSignin",
                "name": "药丸签到服务",
                "trigger": CronTrigger.from_crontab(self._cron),
                "func": self.__signin,
                "kwargs": {}
            })
        return services

    def get_form(self) -> Tuple[List[dict], Dict[str, Any]]:
        return [
            {
                'component': 'VForm',
                'content': [
                    {
                        'component': 'VCard',
                        'props': {'variant': 'outlined', 'class': 'mt-3'},
                        'content': [
                            {
                                'component': 'VCardTitle',
                                'props': {'class': 'd-flex align-center'},
                                'content': [
                                    {'component': 'VIcon', 'props': {'style': 'color: #1976D2;', 'class': 'mr-2'},
                                     'text': 'mdi-calendar-check'},
                                    {'component': 'span', 'text': '药丸签到配置'}
                                ]
                            },
                            {'component': 'VDivider'},
                            {
                                'component': 'VCardText',
                                'content': [
                                    {
                                        'component': 'VRow',
                                        'content': [
                                            {'component': 'VCol', 'props': {'cols': 12, 'md': 4}, 'content': [
                                                {'component': 'VSwitch', 'props': {'model': 'enabled', 'label': '启用插件'}}]},
                                            {'component': 'VCol', 'props': {'cols': 12, 'md': 4}, 'content': [
                                                {'component': 'VSwitch', 'props': {'model': 'notify', 'label': '开启通知'}}]},
                                            {'component': 'VCol', 'props': {'cols': 12, 'md': 4}, 'content': [
                                                {'component': 'VSwitch',
                                                 'props': {'model': 'onlyonce', 'label': '立即运行一次'}}]}
                                        ]
                                    },
                                    {
                                        'component': 'VRow',
                                        'content': [
                                            {
                                                'component': 'VCol',
                                                'props': {'cols': 12},
                                                'content': [
                                                    {
                                                        'component': 'VTextarea',
                                                        'props': {
                                                            'model': 'cookie',
                                                            'label': 'Cookie (flarum_remember)',
                                                            'placeholder': '在此输入从浏览器获取的 flarum_remember 值',
                                                            'hint': '请登录 https://invites.fun/ 后，从浏览器开发者工具中获取 Cookie，并只填写 flarum_remember=... 中等号后面的值。',
                                                            'rows': 3,
                                                            'auto-grow': True
                                                        }
                                                    }
                                                ]
                                            }
                                        ]
                                    },
                                    {
                                        'component': 'VRow',
                                        'content': [
                                            {'component': 'VCol', 'props': {'cols': 12, 'md': 6}, 'content': [
                                                {'component': 'VCronField',
                                                 'props': {'model': 'cron', 'label': '签到周期',
                                                           'placeholder': '30 8 * * *',
                                                           'hint': '五位cron表达式，每天早上8:30执行'}}]},
                                            {'component': 'VCol', 'props': {'cols': 12, 'md': 6}, 'content': [
                                                {'component': 'VTextField',
                                                 'props': {'model': 'history_days', 'label': '历史保留天数',
                                                           'placeholder': '30', 'hint': '历史记录保留天数'}}]}
                                        ]
                                    },
                                    {
                                        'component': 'VRow',
                                        'content': [
                                            {'component': 'VCol', 'props': {'cols': 12, 'md': 6}, 'content': [
                                                {'component': 'VTextField',
                                                 'props': {'model': 'retry_count', 'label': '失败后定时重试次数',
                                                           'type': 'number',
                                                           'placeholder': '0',
                                                           'hint': '0表示不重试，大于0则在签到失败后按间隔时间重试'}}]},
                                            {'component': 'VCol', 'props': {'cols': 12, 'md': 6}, 'content': [
                                                {'component': 'VTextField',
                                                 'props': {'model': 'retry_interval', 'label': '重试间隔(小时)',
                                                           'type': 'number', 'placeholder': '2',
                                                           'hint': '签到失败后多少小时后重试'}}]}
                                        ]
                                    },
                                    {
                                        'component': 'VRow',
                                        'content': [
                                            {'component': 'VCol', 'props': {'cols': 12, 'md': 6}, 'content': [
                                                {'component': 'VSwitch',
                                                 'props': {'model': 'use_proxy', 'label': '使用代理',
                                                           'hint': '与药丸论坛通信时使用系统代理'}}]}
                                        ]
                                    }
                                ]
                            }
                        ]
                    }
                ]
            }
        ], {
            "enabled": False, "notify": True, "cron": "30 8 * * *", "onlyonce": False, "cookie": "",
            "history_days": 30, "retry_count": 0, "retry_interval": 2, "use_proxy": True
        }

    def _get_fa_icon_url(self, icon_class: str) -> Optional[str]:
        """
        将Font Awesome的class转换为CDN的SVG链接
        """
        if not icon_class or ' ' not in icon_class:
            return None
        try:
            parts = icon_class.split(' ')
            prefix = parts[0]
            name = parts[1].replace('fa-', '')

            style_map = {'fas': 'solid', 'far': 'regular', 'fab': 'brands'}
            style_dir = style_map.get(prefix)
            if not style_dir:
                return None

            # 使用稳定的v5版本
            return f"https://cdn.jsdelivr.net/npm/@fortawesome/fontawesome-free@5.15.4/svgs/{style_dir}/{name}.svg"
        except Exception:
            return None

    def get_page(self) -> List[dict]:
        """
        构建插件详情页面，展示签到历史
        """
        history = self.get_data('history') or []
        user_info = self.get_data('user_info')
        user_info_card = None

        # 定义样式，方便复用
        frost_style = 'background-color: rgba(255, 255, 255, 0.75); backdrop-filter: blur(5px); -webkit-backdrop-filter: blur(5px); border: 1px solid rgba(255, 255, 255, 0.2); border-radius: 8px;'
        
        if user_info and 'data' in user_info and 'attributes' in user_info['data']:
            user_attrs = user_info['data']['attributes']
            username = user_attrs.get('displayName', '未知用户')
            avatar_url = user_attrs.get('avatarUrl', '')
            money = user_attrs.get('money', 0)
            discussion_count = user_attrs.get('discussionCount', 0)
            comment_count = user_attrs.get('commentCount', 0)
            follower_count = user_attrs.get('followerCount', 0)
            following_count = user_attrs.get('followingCount', 0)
            last_checkin_time = user_attrs.get('lastCheckinTime', '未知')
            total_continuous_checkin = user_attrs.get('totalContinuousCheckIn', 0)
            join_time = user_attrs.get('joinTime', '')
            last_seen_at = user_attrs.get('lastSeenAt', '')
            background_image = user_attrs.get('decorationProfileBackground') or user_attrs.get('cover')
            badge_count = len(user_info.get('data', {}).get('relationships', {}).get('userBadges', {}).get('data', []))


            if join_time:
                try:
                    join_time = datetime.fromisoformat(join_time.replace('Z', '+00:00')).strftime('%Y-%m-%d')
                except:
                    join_time = '未知'
            if last_seen_at:
                try:
                    last_seen_at = datetime.fromisoformat(last_seen_at.replace('Z', '+00:00')).strftime(
                        '%Y-%m-%d %H:%M')
                except:
                    last_seen_at = '未知'

            groups = []
            included_data = user_info.get('included', [])
            grouped_badges = {}

            if included_data:
                # 1. 预处理 all_included 数据，方便查询
                category_details = {item['id']: item['attributes'] for item in included_data if
                                    item.get('type') == 'badgeCategories'}
                badge_details = {item['id']: item for item in included_data if item.get('type') == 'badges'}
                user_badge_relations = {item['id']: item.get('relationships', {}) for item in included_data if
                                        item.get('type') == 'userBadges'}

                for item in included_data:
                    if item.get('type') == 'groups':
                        group_attrs = item.get('attributes', {})
                        icon_class = group_attrs.get('icon')
                        groups.append({
                            'name': group_attrs.get('nameSingular', ''),
                            'color': group_attrs.get('color', '#888'),
                            'icon_url': self._get_fa_icon_url(icon_class) if icon_class else None
                        })

                # 2. 获取用户拥有的徽章关系
                user_badge_relationship_list = user_info.get('data', {}).get('relationships', {}).get('userBadges', {}).get(
                    'data', [])

                # 3. 遍历用户徽章，查询详情并分组
                for user_badge_rel in user_badge_relationship_list:
                    user_badge_id = user_badge_rel.get('id')
                    if not user_badge_id: continue

                    actual_badge_relationship = user_badge_relations.get(user_badge_id, {}).get('badge', {}).get('data')
                    if not actual_badge_relationship: continue
                    
                    actual_badge_id = actual_badge_relationship.get('id')
                    actual_badge_data = badge_details.get(actual_badge_id)
                    if not actual_badge_data: continue

                    badge_attrs = actual_badge_data.get('attributes', {})
                    
                    category_relationship = actual_badge_data.get('relationships', {}).get('category', {}).get('data')
                    category_id = category_relationship.get('id') if category_relationship else None
                    category_info = category_details.get(category_id, {})
                    category_name = category_info.get('name', '未分类')
                    category_order = category_info.get('order', 99)

                    icon_url = self._get_fa_icon_url(badge_attrs.get('icon'))
                    formatted_badge = {
                        'name': badge_attrs.get('name', '未知徽章'),
                        'icon_url': icon_url,
                        'description': badge_attrs.get('description', '无描述'),
                        'iconColor': badge_attrs.get('iconColor') or '#616161'
                    }

                    if category_name not in grouped_badges:
                        grouped_badges[category_name] = {'order': category_order, 'badges': []}
                    grouped_badges[category_name]['badges'].append(formatted_badge)
            
            # 4. 对分类进行排序
            sorted_grouped_badges = sorted(grouped_badges.items(), key=lambda item: item[1]['order'])

            badge_category_components = []
            if sorted_grouped_badges:
                badge_category_components.append({
                    'component': 'div',
                    'props': {'class': 'd-flex flex-wrap'},
                    'content': [
                        {
                            'component': 'div',
                            'props': {'class': 'ma-1 pa-2', 'style': f'{frost_style} border-radius: 12px;'},
                            'content': [
                                {'component': 'div', 'props': {'class': 'pl-2 text-subtitle-2 grey--text text--darken-1'}, 'text': category_name},
                                {'component': 'VDivider', 'props': {'class': 'my-1'}},
                                {
                                    'component': 'div',
                                    'props': {'class': 'd-flex flex-wrap'},
                                    'content': [
                                        {
                                            'component': 'div',
                                            'props': {
                                                'class': 'ma-1 pa-1 d-flex flex-column align-center',
                                                'style': 'width: 80px; text-align: center;',
                                                'title': badge.get('description', '无描述')
                                            },
                                            'content': [
                                                {
                                                    'component': 'div',
                                                    'props': {
                                                        'class': 'mb-1',
                                                        # 使用 div 配合 mask 避免 VImg 组件的样式覆盖问题
                                                        'style': "height: 20px; width: 20px; -webkit-mask: url('{0}') no-repeat center / contain; mask: url('{0}') no-repeat center / contain; background-color: {1} !important;".format(badge.get('icon_url'), badge.get('iconColor') or '#616161')
                                                    }
                                                } if badge.get('icon_url') else {
                                                    'component': 'VIcon',
                                                    'props': {'icon': 'mdi-emoticon-dead-outline', 'size': '20', 'class': 'mb-1'}
                                                },
                                                {
                                                    'component': 'div',
                                                    'props': {
                                                        'class': 'text-caption',
                                                        'style': 'white-space: normal; line-height: 1.2; font-weight: 500;'
                                                    },
                                                    'text': badge.get('name', '未知徽章')
                                                }
                                            ]
                                        } for badge in data['badges']
                                    ]
                                }
                            ]
                        } for category_name, data in sorted_grouped_badges
                    ]
                })

            user_info_card = {
                'component': 'VCard',
                'props': {'variant': 'outlined', 'class': 'mb-4',
                          'style': f"background-image: url('{background_image}'); background-size: cover; background-position: center;" if background_image else ''},
                'content': [
                    {'component': 'VCardTitle', 'props': {'class': 'd-flex align-center'},
                     'content': [{'component': 'VSpacer'}]},
                    {'component': 'VDivider'},
                    {
                        'component': 'VCardText',
                        'content': [
                            {
                                'component': 'VRow', 'props': {'class': 'ma-1'},
                                'content': [
                                    {
                                        'component': 'VCol', 'props': {'cols': 12, 'md': 5},
                                        'content': [
                                            {
                                                'component': 'div', 'props': {'class': 'd-flex align-center'},
                                                'content': [
                                                    {
                                                        'component': 'div',
                                                        'props': {'class': 'mr-3',
                                                                  'style': 'position: relative; width: 90px; height: 90px;'},
                                                        'content': [
                                                            {'component': 'VAvatar', 'props': {'size': 60,
                                                                                               'rounded': 'circle',
                                                                                               'style': 'position: absolute; top: 15px; left: 15px; z-index: 1;'},
                                                             'content': [{'component': 'VImg', 'props': {
                                                                 'src': avatar_url, 'alt': username}}]},
                                                            {'component': 'div', 'props': {
                                                                'style': f"position: absolute; top: 0; left: 0; width: 90px; height: 90px; background-image: url('{user_attrs.get('decorationAvatarFrame', '')}'); background-size: contain; background-repeat: no-repeat; background-position: center; z-index: 2;"}} if user_attrs.get(
                                                                'decorationAvatarFrame') else {}
                                                        ]
                                                    },
                                                    {
                                                        'component': 'div',
                                                        'content': [
                                                            {'component': 'div', 'props': {
                                                                'class': 'text-h6 mb-1 pa-2 d-inline-block elevation-2',
                                                                'style': frost_style},
                                                             'text': username},
                                                            {'component': 'div',
                                                             'props': {'class': 'd-flex flex-wrap mt-1'},
                                                             'content': [
                                                                 {'component': 'VChip', 'props': {
                                                                     'style': f"background-color: {group.get('color', '#6B7CA8')}; color: white;",
                                                                     'size': 'small', 'class': 'mr-1 mb-1',
                                                                     'variant': 'elevated'}, 
                                                                  'content': [
                                                                        {
                                                                            'component': 'VAvatar',
                                                                            'props': {'start': True, 'size': 16, 'class': 'mr-1'},
                                                                            'content': [{
                                                                                'component': 'div',
                                                                                'props': {
                                                                                    # 使用 div 配合 mask 避免 VImg 组件的样式覆盖问题
                                                                                    'style': "width: 100%; height: 100%; -webkit-mask: url('{0}') no-repeat center / contain; mask: url('{0}') no-repeat center / contain; background-color: white !important;".format(group.get('icon_url'))
                                                                                }
                                                                            }]
                                                                        } if group.get('icon_url') else {
                                                                            'component': 'VIcon',
                                                                            'props': {'start': True, 'size': 'small', 'icon': 'mdi-account-group'}
                                                                        },
                                                                        {'component': 'span', 'text': group.get('name')}
                                                                  ]} for group in groups
                                                             ]}
                                                        ]
                                                    }
                                                ]
                                            },
                                            {
                                                'component': 'VRow', 'props': {'class': 'mt-2'},
                                                'content': [
                                                    {
                                                        'component': 'VCol', 'props': {'cols': 12},
                                                        'content': [
                                                            {'component': 'div', 'props': {
                                                                'class': 'pa-1 elevation-2 mb-1',
                                                                'style': f'{frost_style} width: fit-content;'},
                                                             'content': [{'component': 'div', 'props': {
                                                                 'class': 'd-flex align-center text-caption'},
                                                                          'content': [
                                                                              {'component': 'VIcon', 'props': {
                                                                                  'style': 'color: #4CAF50;',
                                                                                  'size': 'x-small',
                                                                                  'class': 'mr-1'},
                                                                               'text': 'mdi-calendar'},
                                                                              {'component': 'span',
                                                                               'text': f'注册于 {join_time}'}]}]},
                                                            {'component': 'div', 'props': {
                                                                'class': 'pa-1 elevation-2 mb-1',
                                                                'style': f'{frost_style} width: fit-content;'},
                                                             'content': [{'component': 'div', 'props': {
                                                                 'class': 'd-flex align-center text-caption'},
                                                                          'content': [
                                                                              {'component': 'VIcon', 'props': {
                                                                                  'style': 'color: #2196F3;',
                                                                                  'size': 'x-small',
                                                                                  'class': 'mr-1'},
                                                                               'text': 'mdi-clock-outline'},
                                                                              {'component': 'span',
                                                                               'text': f'最后访问 {last_seen_at}'}]}]},
                                                            {'component': 'div', 'props': {
                                                                'class': 'pa-1 elevation-2',
                                                                'style': f'{frost_style} width: fit-content;'},
                                                             'content': [{'component': 'div', 'props': {
                                                                 'class': 'd-flex align-center text-caption'},
                                                                          'content': [
                                                                              {'component': 'VIcon', 'props': {
                                                                                  'style': 'color: #E64A19;',
                                                                                  'size': 'x-small',
                                                                                  'class': 'mr-1'},
                                                                               'text': 'mdi-medal-outline'},
                                                                              {'component': 'span',
                                                                               'text': f'拥有 {badge_count} 枚徽章'}]}]}
                                                        ]
                                                    }
                                                ]
                                            }
                                        ]
                                    },
                                    {
                                        'component': 'VCol', 'props': {'cols': 12, 'md': 7},
                                        'content': [
                                            {
                                                'component': 'VRow',
                                                'content': [
                                                    {'component': 'VCol', 'props': {'cols': 6, 'sm': 4},
                                                     'content': [
                                                         {'component': 'div', 'props': {
                                                             'class': 'text-center pa-2 elevation-2',
                                                             'style': frost_style},
                                                          'content': [{'component': 'div', 'props': {
                                                              'class': 'd-flex justify-center align-center'},
                                                                      'content': [
                                                                          {'component': 'VIcon', 'props': {
                                                                              'style': 'color: #FFC107;',
                                                                              'class': 'mr-1'},
                                                                           'text': 'mdi-pill'},
                                                                          {'component': 'span', 'props': {
                                                                              'class': 'text-h6'},
                                                                           'text': str(money)}]},
                                                                     {'component': 'div',
                                                                      'props': {
                                                                          'class': 'text-caption mt-1'},
                                                                      'text': '药丸'}]}]},
                                                    {'component': 'VCol', 'props': {'cols': 6, 'sm': 4},
                                                     'content': [
                                                         {'component': 'div', 'props': {
                                                             'class': 'text-center pa-2 elevation-2',
                                                             'style': frost_style},
                                                          'content': [{'component': 'div', 'props': {
                                                              'class': 'd-flex justify-center align-center'},
                                                                      'content': [
                                                                          {'component': 'VIcon', 'props': {
                                                                              'style': 'color: #3F51B5;',
                                                                              'class': 'mr-1'},
                                                                           'text': 'mdi-forum'},
                                                                          {'component': 'span', 'props': {
                                                                              'class': 'text-h6'},
                                                                           'text': str(discussion_count)}]},
                                                                     {'component': 'div',
                                                                      'props': {
                                                                          'class': 'text-caption mt-1'},
                                                                      'text': '主题'}]}]},
                                                    {'component': 'VCol', 'props': {'cols': 6, 'sm': 4},
                                                     'content': [
                                                         {'component': 'div', 'props': {
                                                             'class': 'text-center pa-2 elevation-2',
                                                             'style': frost_style},
                                                          'content': [{'component': 'div', 'props': {
                                                              'class': 'd-flex justify-center align-center'},
                                                                      'content': [
                                                                          {'component': 'VIcon', 'props': {
                                                                              'style': 'color: #00BCD4;',
                                                                              'class': 'mr-1'},
                                                                           'text': 'mdi-comment-text-multiple'},
                                                                          {'component': 'span', 'props': {
                                                                              'class': 'text-h6'},
                                                                           'text': str(comment_count)}]},
                                                                     {'component': 'div',
                                                                      'props': {
                                                                          'class': 'text-caption mt-1'},
                                                                      'text': '评论'}]}]},
                                                    {'component': 'VCol', 'props': {'cols': 6, 'sm': 4},
                                                     'content': [
                                                         {'component': 'div', 'props': {
                                                             'class': 'text-center pa-2 elevation-2',
                                                             'style': frost_style},
                                                          'content': [{'component': 'div', 'props': {
                                                              'class': 'd-flex justify-center align-center'},
                                                                      'content': [
                                                                          {'component': 'VIcon', 'props': {
                                                                              'style': 'color: #673AB7;',
                                                                              'class': 'mr-1'},
                                                                           'text': 'mdi-account-group'},
                                                                          {'component': 'span', 'props': {
                                                                              'class': 'text-h6'},
                                                                           'text': str(follower_count)}]},
                                                                     {'component': 'div',
                                                                      'props': {
                                                                          'class': 'text-caption mt-1'},
                                                                      'text': '粉丝'}]}]},
                                                    {'component': 'VCol', 'props': {'cols': 6, 'sm': 4},
                                                     'content': [
                                                         {'component': 'div', 'props': {
                                                             'class': 'text-center pa-2 elevation-2',
                                                             'style': frost_style},
                                                          'content': [{'component': 'div', 'props': {
                                                              'class': 'd-flex justify-center align-center'},
                                                                      'content': [
                                                                          {'component': 'VIcon', 'props': {
                                                                              'style': 'color: #03A9F4;',
                                                                              'class': 'mr-1'},
                                                                           'text': 'mdi-account-multiple-plus'},
                                                                          {'component': 'span', 'props': {
                                                                              'class': 'text-h6'},
                                                                           'text': str(following_count)}]},
                                                                     {'component': 'div',
                                                                      'props': {
                                                                          'class': 'text-caption mt-1'},
                                                                      'text': '关注'}]}]},
                                                    {'component': 'VCol', 'props': {'cols': 12, 'sm': 4},
                                                     'content': [
                                                         {'component': 'div', 'props': {
                                                             'class': 'text-center pa-2 elevation-2',
                                                             'style': frost_style},
                                                          'content': [{'component': 'div', 'props': {
                                                              'class': 'd-flex justify-center align-center'},
                                                                      'content': [
                                                                          {'component': 'VIcon', 'props': {
                                                                              'style': 'color: #009688;',
                                                                              'class': 'mr-1'},
                                                                           'text': 'mdi-calendar-check'},
                                                                          {'component': 'span', 'props': {
                                                                              'class': 'text-h6'},
                                                                           'text': str(
                                                                               total_continuous_checkin)}]},
                                                                     {'component': 'div',
                                                                      'props': {
                                                                          'class': 'text-caption mt-1'},
                                                                      'text': '连续签到'}]}]}
                                                ]
                                            }
                                        ]
                                    }
                                ]
                            },
                            # 徽章分类显示区域
                            *badge_category_components,
                            # 最后签到时间
                            {'component': 'div', 'props': {
                                'class': 'mt-2 text-caption text-right pa-1 elevation-2 d-inline-block float-right',
                                'style': frost_style},
                             'text': f'最后签到: {last_checkin_time}'}
                        ]
                    }
                ]
            }

            components = []
            if user_info_card:
                components.append(user_info_card)

            if not history:
                components.extend([
                    {'component': 'VAlert', 'props': {'type': 'info', 'variant': 'tonal',
                                                      'text': '暂无签到记录，请先配置Cookie并启用插件',
                                                      'class': 'mb-2', 'prepend-icon': 'mdi-information'}},
                    {'component': 'VCard', 'props': {'variant': 'outlined', 'class': 'mb-4'}, 'content': [
                        {'component': 'VCardTitle', 'props': {'class': 'd-flex align-center'}, 'content': [
                            {'component': 'VIcon', 'props': {'color': 'amber-darken-2', 'class': 'mr-2'},
                             'text': 'mdi-pill'},
                            {'component': 'span', 'props': {'class': 'text-h6'}, 'text': '签到奖励说明'}]},
                        {'component': 'VDivider'}, {'component': 'VCardText', 'props': {'class': 'pa-3'}, 'content': [
                            {'component': 'div', 'props': {'class': 'd-flex align-center mb-2'}, 'content': [
                                {'component': 'VIcon',
                                 'props': {'style': 'color: #FF8F00;', 'size': 'small', 'class': 'mr-2'},
                                 'text': 'mdi-check-circle'}, {'component': 'span', 'text': '每日签到可获得随机药丸奖励'}]},
                            {'component': 'div', 'props': {'class': 'd-flex align-center'}, 'content': [
                                {'component': 'VIcon',
                                 'props': {'style': 'color: #1976D2;', 'size': 'small', 'class': 'mr-2'},
                                 'text': 'mdi-calendar-check'},
                                {'component': 'span', 'text': '连续签到可累计天数，提升论坛等级'}]}]}]}
                ])
                return components

            history = sorted(history, key=lambda x: x.get("date", ""), reverse=True)
            history_rows = []
            for record in history:
                status_text = record.get("status", "未知")
                status_color = "success" if "成功" in status_text or "已签到" in status_text else "error"
                status_icon = "mdi-check-circle" if status_color == 'success' else "mdi-close-circle"
                history_rows.append({
                    'component': 'tr',
                    'content': [
                        {'component': 'td', 'props': {'class': 'text-caption'}, 'text': record.get("date", "")},
                        {'component': 'td', 'content': [{'component': 'VChip', 'props': {
                            'style': 'background-color: #4CAF50; color: white;' if status_color == 'success' else 'background-color: #F44336; color: white;',
                            'size': 'small', 'variant': 'elevated'}, 'content': [
                            {'component': 'VIcon',
                             'props': {'start': True, 'style': 'color: white;', 'size': 'small'},
                             'text': status_icon}, {'component': 'span', 'text': status_text}]}, {
                                                            'component': 'div',
                                                            'props': {'class': 'mt-1 text-caption grey--text'},
                                                            'text': f"将在{record.get('retry', {}).get('interval', self._retry_interval)}小时后重试 ({record.get('retry', {}).get('current', 0)}/{record.get('retry', {}).get('max', self._retry_count)})" if status_color == 'error' and record.get('retry', {}).get('enabled', False) and record.get(
                                                                'retry',
                                                                {}).get(
                                                                'current', 0) > 0 else ""}]},
                        {'component': 'td',
                         'content': [{'component': 'div', 'props': {'class': 'd-flex align-center'},
                                      'content': [{'component': 'VIcon',
                                                   'props': {'style': 'color: #FFC107;',
                                                             'class': 'mr-1'},
                                                   'text': 'mdi-pill'},
                                                  {'component': 'span',
                                                   'text': record.get('money', '—')}]}]},
                        {'component': 'td',
                         'content': [{'component': 'div', 'props': {'class': 'd-flex align-center'},
                                      'content': [{'component': 'VIcon',
                                                   'props': {'style': 'color: #1976D2;',
                                                             'class': 'mr-1'},
                                                   'text': 'mdi-calendar-check'},
                                                  {'component': 'span', 'text': record.get(
                                                      'totalContinuousCheckIn', '—')}]}]},
                        {'component': 'td',
                         'content': [{'component': 'div', 'props': {'class': 'd-flex align-center'},
                                      'content': [{'component': 'VIcon',
                                                   'props': {'style': 'color: #FF8F00;',
                                                             'class': 'mr-1'},
                                                   'text': 'mdi-gift'}, {'component': 'span',
                                                                         'text': f"{record.get('lastCheckinMoney', 0)}药丸" if (
                                                                                             "成功" in status_text or "已签到" in status_text) and record.get(
                                                                                     'lastCheckinMoney',
                                                                                     0) > 0 else '—'}]}]},
                        {'component': 'td', 'content': [
                            {'component': 'span', 'text': record.get('fail_count', 0)}
                        ]}
                    ]
                })

            components.append({
                'component': 'VCard', 'props': {'variant': 'outlined', 'class': 'mb-4'},
                'content': [
                    {'component': 'VCardTitle', 'props': {'class': 'd-flex align-center'}, 'content': [
                        {'component': 'VIcon', 'props': {'style': 'color: #9C27B0;', 'class': 'mr-2'},
                         'text': 'mdi-calendar-check'},
                        {'component': 'span', 'props': {'class': 'text-h6 font-weight-bold'}, 'text': '药丸签到历史'},
                        {'component': 'VSpacer'}, {'component': 'VChip', 'props': {
                            'style': 'background-color: #FF9800; color: white;', 'size': 'small',
                            'variant': 'elevated'}, 'content': [
                            {'component': 'VIcon',
                             'props': {'start': True, 'style': 'color: white;', 'size': 'small'},
                             'text': 'mdi-pill'}, {'component': 'span', 'text': '每日可得药丸奖励'}]}]},
                    {'component': 'VDivider'},
                    {'component': 'VCardText', 'props': {'class': 'pa-2'}, 'content': [
                        {'component': 'VTable', 'props': {'hover': True, 'density': 'comfortable'}, 'content': [
                            {'component': 'thead', 'content': [{'component': 'tr', 'content': [
                                {'component': 'th', 'text': '时间'}, {'component': 'th', 'text': '状态'},
                                {'component': 'th', 'text': '药丸'}, {'component': 'th', 'text': '签到天数'},
                                {'component': 'th', 'text': '奖励'}, {'component': 'th', 'text': '失败次数'}]}]},
                            {'component': 'tbody', 'content': history_rows}]}]}
                ]
            })
            components.append({'component': 'style',
                                'text': ".v-table { border-radius: 8px; overflow: hidden; box-shadow: 0 1px 2px rgba(0,0,0,0.05); } .v-table th { background-color: rgba(var(--v-theme-primary), 0.05); color: rgb(var(--v-theme-primary)); font-weight: 600; }"})

            return components

    def stop_service(self):
        try:
            if self._scheduler:
                self._scheduler.remove_all_jobs()
                if self._scheduler.running:
                    self._scheduler.shutdown()
                self._scheduler = None
        except Exception as e:
            logger.error("退出插件失败：%s" % str(e))
# 确保复制到这一行结束
