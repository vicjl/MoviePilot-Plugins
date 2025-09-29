import json
import re
import time
from datetime import datetime, timedelta

import pytz
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from app.core.config import settings
from app.plugins import _PluginBase
from typing import Any, List, Dict, Tuple, Optional
from app.log import logger
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
    plugin_version = "1.0.0"
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

        # 停止现有任务
        self.stop_service()

        # 确保scheduler是新的
        self._scheduler = BackgroundScheduler(timezone=settings.TZ)

        # 立即运行一次
        if self._onlyonce:
            logger.info(f"药丸签到服务启动，立即运行一次")
            self._scheduler.add_job(func=self.__signin, trigger='date',
                                    run_date=datetime.now(tz=pyytz.timezone(settings.TZ)) + timedelta(seconds=3),
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
        elif self._cron:
            logger.info(f"药丸签到服务启动，周期：{self._cron}")
            self._scheduler.add_job(func=self.__signin,
                                    trigger=CronTrigger.from_crab(self._cron),
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
            remaining_retries = self._retry_count - self._current_retry
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
        获取代理设置
        """
        if not self._use_proxy:
            logger.info("未启用代理")
            return None

        try:
            # 获取系统代理设置
            if hasattr(settings, 'PROXY') and settings.PROXY:
                logger.info(f"使用系统代理: {settings.PROXY}")
                return settings.PROXY
            else:
                logger.warning("系统代理未配置")
                return None
        except Exception as e:
            logger.error(f"获取代理设置出错: {str(e)}")
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
                            f"• 在插件设置中填写药丸论坛的 flarum_remember Cookie\n"
                            f"━━━━━━━━━━"
                        )
                    )
                return False

            # 使用循环而非递归实现重试
            for attempt in range(max_retries + 1):
                if attempt > 0:
                    logger.info(f"正在进行第 {attempt}/{max_retries} 次重试...")
                    time.sleep(3)  # 重试前等待3秒

                # 获取代理设置
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
                    logger.info(f"药丸已签到，当前魔力: {money}，累计签到: {totalContinuousCheckIn}")
                else:
                    status_text = "签到成功"
                    reward_text = f"获得{lastCheckinMoney}魔力奖励"
                    logger.info(f"药丸签到成功，获得{lastCheckinMoney}魔力，当前魔力: {money}，累计签到: {totalContinuousCheckIn}")

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
                            f"🪄 魔力：{money}\n"
                            f"📆 签到天数：{totalContinuousCheckIn}\n"
                            f"━━━━━━━━━━"
                        )
                    )

                history = {
                    "date": datetime.today().strftime('%Y-%m-%d %H:%M:%S'),
                    "status": status_text,
                    "money": money,
                    "totalContinuousCheckIn": totalContinuousCheckIn,
                    "lastCheckinMoney": lastCheckinMoney,
                    "retry": {
                        "enabled": self._retry_count > 0,
                        "current": self._current_retry,
                        "max": self._retry_count,
                        "interval": self._retry_interval
                    }
                }

                self._save_history(history)

                if self._current_retry > 0:
                    logger.info(f"药丸签到重试成功，重置重试计数")
                    self._current_retry = 0

                return True

        except Exception as e:
            logger.error(f"签到过程发生异常: {str(e)}")
            import traceback
            logger.error(f"错误详情: {traceback.format_exc()}")

            self._send_signin_failure_notification(str(e), attempt)

            if self._retry_count > 0 and self._current_retry < self._retry_count:
                self._current_retry += 1
                retry_hours = self._retry_interval
                logger.info(f"安排第{self._current_retry}次定时重试，将在{retry_hours}小时后重试")
                self._schedule_retry(hours=retry_hours)
            else:
                if self._retry_count > 0:
                    logger.info("已达到最大定时重试次数，不再重试")
                self._current_retry = 0

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
                "max": self._retry_count,
                "interval": self._retry_interval
            }
        
        history.append(record)
        
        if self._history_days:
            try:
                thirty_days_ago = time.time() - int(self._history_days) * 24 * 60 * 60
                history = [record for record in history if
                          datetime.strptime(record["date"],
                                         '%Y-%m-%d %H:%M:%S').timestamp() >= thirty_days_ago]
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
                                    {'component': 'VIcon', 'props': {'style': 'color: #1976D2;', 'class': 'mr-2'}, 'text': 'mdi-calendar-check'},
                                    {'component': 'span', 'text': '药丸签到设置'}
                                ]
                            },
                            {'component': 'VDivider'},
                            {
                                'component': 'VCardText',
                                'content': [
                                    {
                                        'component': 'VRow',
                                        'content': [
                                            {'component': 'VCol', 'props': {'cols': 12, 'md': 4}, 'content': [{'component': 'VSwitch', 'props': {'model': 'enabled', 'label': '启用插件'}}]},
                                            {'component': 'VCol', 'props': {'cols': 12, 'md': 4}, 'content': [{'component': 'VSwitch', 'props': {'model': 'notify', 'label': '开启通知'}}]},
                                            {'component': 'VCol', 'props': {'cols': 12, 'md': 4}, 'content': [{'component': 'VSwitch', 'props': {'model': 'onlyonce', 'label': '立即运行一次'}}]}
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
                                            {'component': 'VCol', 'props': {'cols': 12, 'md': 6}, 'content': [{'component': 'VCronField', 'props': {'model': 'cron', 'label': '签到周期', 'placeholder': '30 8 * * *', 'hint': '五位cron表达式，每天早上8:30执行'}}]},
                                            {'component': 'VCol', 'props': {'cols': 12, 'md': 6}, 'content': [{'component': 'VTextField', 'props': {'model': 'history_days', 'label': '历史保留天数', 'placeholder': '30', 'hint': '历史记录保留天数'}}]}
                                        ]
                                    },
                                    {
                                        'component': 'VRow',
                                        'content': [
                                            {'component': 'VCol', 'props': {'cols': 12, 'md': 6}, 'content': [{'component': 'VTextField', 'props': {'model': 'retry_count', 'label': '失败重试次数', 'type': 'number', 'placeholder': '0', 'hint': '0表示不重试，大于0则在签到失败后重试'}}]},
                                            {'component': 'VCol', 'props': {'cols': 12, 'md': 6}, 'content': [{'component': 'VTextField', 'props': {'model': 'retry_interval', 'label': '重试间隔(小时)', 'type': 'number', 'placeholder': '2', 'hint': '签到失败后多少小时后重试'}}]}
                                        ]
                                    },
                                    {
                                        'component': 'VRow',
                                        'content': [
                                            {'component': 'VCol', 'props': {'cols': 12, 'md': 6}, 'content': [{'component': 'VSwitch', 'props': {'model': 'use_proxy', 'label': '使用代理', 'hint': '与药丸论坛通信时使用系统代理'}}]}
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

    def get_page(self) -> List[dict]:
        history = self.get_data('history') or []
        user_info = self.get_data('user_info')
        user_info_card = None
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

            if join_time:
                try: join_time = datetime.fromisoformat(join_time.replace('Z', '+00:00')).strftime('%Y-%m-%d')
                except: join_time = '未知'
            if last_seen_at:
                try: last_seen_at = datetime.fromisoformat(last_seen_at.replace('Z', '+00:00')).strftime('%Y-%m-%d %H:%M')
                except: last_seen_at = '未知'
            
            groups = []
            if 'included' in user_info:
                for item in user_info.get('included', []):
                    if item.get('type') == 'groups':
                        groups.append({'name': item.get('attributes', {}).get('nameSingular', ''), 'color': item.get('attributes', {}).get('color', '#888'), 'icon': item.get('attributes', {}).get('icon', '')})
            
            badges = []
            user_badges_data = user_attrs.get('badges', [])
            for badge_item in user_badges_data:
                core_badge_info = badge_item.get('badge', {})
                if not core_badge_info: continue
                category_info = core_badge_info.get('category', {})
                badges.append({'name': core_badge_info.get('name', '未知徽章'), 'icon': core_badge_info.get('icon', 'fas fa-award'), 'description': core_badge_info.get('description', '无描述'), 'backgroundColor': core_badge_info.get('backgroundColor'), 'iconColor': core_badge_info.get('iconColor'), 'category': category_info.get('name', '其他')})
            
            user_info_card = {
                'component': 'VCard',
                'props': {'variant': 'outlined', 'class': 'mb-4', 'style': f"background-image: url('{background_image}'); background-size: cover; background-position: center;" if background_image else ''},
                'content': [
                    {'component': 'VCardTitle', 'props': {'class': 'd-flex align-center'}, 'content': [{'component': 'VSpacer'}]},
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
                                                        'component': 'div', 'props': {'class': 'mr-3', 'style': 'position: relative; width: 90px; height: 90px;'},
                                                        'content': [
                                                            {'component': 'VAvatar', 'props': {'size': 60, 'rounded': 'circle', 'style': 'position: absolute; top: 15px; left: 15px; z-index: 1;'}, 'content': [{'component': 'VImg', 'props': {'src': avatar_url, 'alt': username}}]},
                                                            {'component': 'div', 'props': {'style': f"position: absolute; top: 0; left: 0; width: 90px; height: 90px; background-image: url('{user_attrs.get('decorationAvatarFrame', '')}'); background-size: contain; background-repeat: no-repeat; background-position: center; z-index: 2;"}} if user_attrs.get('decorationAvatarFrame') else {}
                                                        ]
                                                    },
                                                    {
                                                        'component': 'div',
                                                        'content': [
                                                            {'component': 'div', 'props': {'class': 'text-h6 mb-1 pa-1 d-inline-block elevation-1', 'style': 'background-color: rgba(255, 255, 255, 0.6); border-radius: 4px;'}, 'text': username},
                                                            {'component': 'div', 'props': {'class': 'd-flex flex-wrap mt-1'}, 'content': [{'component': 'VChip', 'props': {'style': f"background-color: {group.get('color', '#6B7CA8')}; color: white; padding: 0 8px; min-width: 60px; border-radius: 2px; height: 32px;", 'size': 'small', 'class': 'mr-1 mb-1', 'variant': 'elevated'}, 'content': [{'component': 'VIcon', 'props': {'start': True, 'size': 'small', 'style': 'margin-right: 3px;'}, 'text': group.get('icon') or 'mdi-account'}, {'component': 'span', 'text': group.get('name')}]} for group in groups]}
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
                                                            {'component': 'div', 'props': {'class': 'pa-1 elevation-1 mb-1 ml-0', 'style': 'background-color: rgba(255, 255, 255, 0.6); border-radius: 4px; width: fit-content;'}, 'content': [{'component': 'div', 'props': {'class': 'd-flex align-center text-caption'}, 'content': [{'component': 'VIcon', 'props': {'style': 'color: #4CAF50;', 'size': 'x-small', 'class': 'mr-1'}, 'text': 'mdi-calendar'}, {'component': 'span', 'text': f'注册于 {join_time}'}]}]},
                                                            {'component': 'div', 'props': {'class': 'pa-1 elevation-1', 'style': 'background-color: rgba(255, 255, 255, 0.6); border-radius: 4px; width: fit-content;'}, 'content': [{'component': 'div', 'props': {'class': 'd-flex align-center text-caption'}, 'content': [{'component': 'VIcon', 'props': {'style': 'color: #2196F3;', 'size': 'x-small', 'class': 'mr-1'}, 'text': 'mdi-clock-outline'}, {'component': 'span', 'text': f'最后访问 {last_seen_at}'}]}]}
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
                                                    {'component': 'VCol', 'props': {'cols': 6, 'md': 4}, 'content': [{'component': 'div', 'props': {'class': 'text-center pa-1 elevation-1', 'style': 'background-color: rgba(255, 255, 255, 0.6); border-radius: 4px;'}, 'content': [{'component': 'div', 'props': {'class': 'd-flex justify-center align-center'}, 'content': [{'component': 'VIcon', 'props': {'style': 'color: #FFC107;', 'class': 'mr-1'}, 'text': 'mdi-star-four-points'}, {'component': 'span', 'props': {'class': 'text-h6'}, 'text': str(money)}]}, {'component': 'div', 'props': {'class': 'text-caption mt-1'}, 'text': '魔力'}]}]},
                                                    {'component': 'VCol', 'props': {'cols': 6, 'md': 4}, 'content': [{'component': 'div', 'props': {'class': 'text-center pa-1 elevation-1', 'style': 'background-color: rgba(255, 255, 255, 0.6); border-radius: 4px;'}, 'content': [{'component': 'div', 'props': {'class': 'd-flex justify-center align-center'}, 'content': [{'component': 'VIcon', 'props': {'style': 'color: #3F51B5;', 'class': 'mr-1'}, 'text': 'mdi-forum'}, {'component': 'span', 'props': {'class': 'text-h6'}, 'text': str(discussion_count)}]}, {'component': 'div', 'props': {'class': 'text-caption mt-1'}, 'text': '主题'}]}]},
                                                    {'component': 'VCol', 'props': {'cols': 6, 'md': 4}, 'content': [{'component': 'div', 'props': {'class': 'text-center pa-1 elevation-1', 'style': 'background-color: rgba(255, 255, 255, 0.6); border-radius: 4px;'}, 'content': [{'component': 'div', 'props': {'class': 'd-flex justify-center align-center'}, 'content': [{'component': 'VIcon', 'props': {'style': 'color: #00BCD4;', 'class': 'mr-1'}, 'text': 'mdi-comment-text-multiple'}, {'component': 'span', 'props': {'class': 'text-h6'}, 'text': str(comment_count)}]}, {'component': 'div', 'props': {'class': 'text-caption mt-1'}, 'text': '评论'}]}]},
                                                    {'component': 'VCol', 'props': {'cols': 6, 'md': 4}, 'content': [{'component': 'div', 'props': {'class': 'text-center pa-1 elevation-1', 'style': 'background-color: rgba(255, 255, 255, 0.6); border-radius: 4px;'}, 'content': [{'component': 'div', 'props': {'class': 'd-flex justify-center align-center'}, 'content': [{'component': 'VIcon', 'props': {'style': 'color: #673AB7;', 'class': 'mr-1'}, 'text': 'mdi-account-group'}, {'component': 'span', 'props': {'class': 'text-h6'}, 'text': str(follower_count)}]}, {'component': 'div', 'props': {'class': 'text-caption mt-1'}, 'text': '粉丝'}]}]},
                                                    {'component': 'VCol', 'props': {'cols': 6, 'md': 4}, 'content': [{'component': 'div', 'props': {'class': 'text-center pa-1 elevation-1', 'style': 'background-color: rgba(255, 255, 255, 0.6); border-radius: 4px;'}, 'content': [{'component': 'div', 'props': {'class': 'd-flex justify-center align-center'}, 'content': [{'component': 'VIcon', 'props': {'style': 'color: #03A9F4;', 'class': 'mr-1'}, 'text': 'mdi-account-multiple-plus'}, {'component': 'span', 'props': {'class': 'text-h6'}, 'text': str(following_count)}]}, {'component': 'div', 'props': {'class': 'text-caption mt-1'}, 'text': '关注'}]}]},
                                                    {'component': 'VCol', 'props': {'cols': 6, 'md': 4}, 'content': [{'component': 'div', 'props': {'class': 'text-center pa-1 elevation-1', 'style': 'background-color: rgba(255, 255, 255, 0.6); border-radius: 4px;'}, 'content': [{'component': 'div', 'props': {'class': 'd-flex justify-center align-center'}, 'content': [{'component': 'VIcon', 'props': {'style': 'color: #009688;', 'class': 'mr-1'}, 'text': 'mdi-calendar-check'}, {'component': 'span', 'props': {'class': 'text-h6'}, 'text': str(total_continuous_checkin)}]}, {'component': 'div', 'props': {'class': 'text-caption mt-1'}, 'text': '连续签到'}]}]}
                                                ]
                                            }
                                        ]
                                    }
                                ]
                            },
                            {
                                'component': 'div', 'props': {'class': 'mb-1 mt-1 pl-0'},
                                'content': [
                                    {'component': 'div', 'props': {'class': 'd-flex align-center mb-1 elevation-1 d-inline-block ml-0', 'style': 'background-color: rgba(255, 255, 255, 0.6); border-radius: 3px; width: fit-content; padding: 2px 8px 2px 5px;'}, 'content': [{'component': 'VIcon', 'props': {'style': 'color: #FFA000;', 'class': 'mr-1', 'size': 'small'}, 'text': 'mdi-medal'}, {'component': 'span', 'props': {'class': 'text-body-2 font-weight-medium'}, 'text': f'徽章({len(badges)})'}]},
                                    {'component': 'div', 'props': {'class': 'd-flex flex-wrap'}, 'content': [{'component': 'VChip', 'props': {'class': 'ma-1', 'variant': 'tonal', 'color': badge.get('backgroundColor'), 'title': badge.get('description', '无描述')}, 'content': [{'component': 'VIcon', 'props': {'start': True, 'icon': badge.get('icon'), 'color': badge.get('iconColor')}}, {'component': 'span', 'text': badge.get('name', '未知徽章')}]} for badge in badges]}
                                ]
                            },
                            {'component': 'div', 'props': {'class': 'mt-1 text-caption text-right grey--text pa-1 elevation-1 d-inline-block float-right', 'style': 'background-color: rgba(255, 255, 255, 0.6); border-radius: 4px;'}, 'text': f'最后签到: {last_checkin_time}'}
                        ]
                    }
                ]
            }

        components = []
        if user_info_card:
            components.append(user_info_card)

        if not history:
            components.extend([
                {'component': 'VAlert', 'props': {'type': 'info', 'variant': 'tonal', 'text': '暂无签到记录，请先配置Cookie并启用插件', 'class': 'mb-2', 'prepend-icon': 'mdi-information'}},
                {'component': 'VCard', 'props': {'variant': 'outlined', 'class': 'mb-4'}, 'content': [{'component': 'VCardTitle', 'props': {'class': 'd-flex align-center'}, 'content': [{'component': 'VIcon', 'props': {'color': 'amber-darken-2', 'class': 'mr-2'}, 'text': 'mdi-star-four-points'}, {'component': 'span', 'props': {'class': 'text-h6'}, 'text': '签到奖励说明'}]}, {'component': 'VDivider'}, {'component': 'VCardText', 'props': {'class': 'pa-3'}, 'content': [{'component': 'div', 'props': {'class': 'd-flex align-center mb-2'}, 'content': [{'component': 'VIcon', 'props': {'style': 'color: #FF8F00;', 'size': 'small', 'class': 'mr-2'}, 'text': 'mdi-check-circle'}, {'component': 'span', 'text': '每日签到可获得随机魔力奖励'}]}, {'component': 'div', 'props': {'class': 'd-flex align-center'}, 'content': [{'component': 'VIcon', 'props': {'style': 'color: #1976D2;', 'size': 'small', 'class': 'mr-2'}, 'text': 'mdi-calendar-check'}, {'component': 'span', 'text': '连续签到可累积天数，提升论坛等级'}]}]}]}
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
                    {'component': 'td', 'content': [{'component': 'VChip', 'props': {'style': 'background-color: #4CAF50; color: white;' if status_color == 'success' else 'background-color: #F44336; color: white;', 'size': 'small', 'variant': 'elevated'}, 'content': [{'component': 'VIcon', 'props': {'start': True, 'style': 'color: white;', 'size': 'small'}, 'text': status_icon}, {'component': 'span', 'text': status_text}]}, {'component': 'div', 'props': {'class': 'mt-1 text-caption grey--text'}, 'text': f"将在{record.get('retry', {}).get('interval', self._retry_interval)}小时后重试 ({record.get('retry', {}).get('current', 0)}/{record.get('retry', {}).get('max', self._retry_count)})" if status_color == 'error' and record.get('retry', {}).get('enabled', False) and record.get('retry', {}).get('current', 0) > 0 else ""}]},
                    {'component': 'td', 'content': [{'component': 'div', 'props': {'class': 'd-flex align-center'}, 'content': [{'component': 'VIcon', 'props': {'style': 'color: #FFC107;', 'class': 'mr-1'}, 'text': 'mdi-star-four-points'}, {'component': 'span', 'text': record.get('money', '—')}]}]},
                    {'component': 'td', 'content': [{'component': 'div', 'props': {'class': 'd-flex align-center'}, 'content': [{'component': 'VIcon', 'props': {'style': 'color: #1976D2;', 'class': 'mr-1'}, 'text': 'mdi-calendar-check'}, {'component': 'span', 'text': record.get('totalContinuousCheckIn', '—')}]}]},
                    {'component': 'td', 'content': [{'component': 'div', 'props': {'class': 'd-flex align-center'}, 'content': [{'component': 'VIcon', 'props': {'style': 'color: #FF8F00;', 'class': 'mr-1'}, 'text': 'mdi-gift'}, {'component': 'span', 'text': f"{record.get('lastCheckinMoney', 0)}魔力" if ("成功" in status_text or "已签到" in status_text) and record.get('lastCheckinMoney', 0) > 0 else '—'}]}]}
                ]
            })

        components.append({
            'component': 'VCard', 'props': {'variant': 'outlined', 'class': 'mb-4'},
            'content': [
                {'component': 'VCardTitle', 'props': {'class': 'd-flex align-center'}, 'content': [{'component': 'VIcon', 'props': {'style': 'color: #9C27B0;', 'class': 'mr-2'}, 'text': 'mdi-calendar-check'}, {'component': 'span', 'props': {'class': 'text-h6 font-weight-bold'}, 'text': '药丸签到历史'}, {'component': 'VSpacer'}, {'component': 'VChip', 'props': {'style': 'background-color: #FF9800; color: white;', 'size': 'small', 'variant': 'elevated'}, 'content': [{'component': 'VIcon', 'props': {'start': True, 'style': 'color: white;', 'size': 'small'}, 'text': 'mdi-star-four-points'}, {'component': 'span', 'text': '每日可得魔力奖励'}]}]},
                {'component': 'VDivider'},
                {'component': 'VCardText', 'props': {'class': 'pa-2'}, 'content': [{'component': 'VTable', 'props': {'hover': True, 'density': 'comfortable'}, 'content': [{'component': 'thead', 'content': [{'component': 'tr', 'content': [{'component': 'th', 'text': '时间'}, {'component': 'th', 'text': '状态'}, {'component': 'th', 'text': '魔力'}, {'component': 'th', 'text': '签到天数'}, {'component': 'th', 'text': '奖励'}]}]}, {'component': 'tbody', 'content': history_rows}]}]}
            ]
        })
        components.append({'component': 'style', 'text': ".v-table { border-radius: 8px; overflow: hidden; box-shadow: 0 1px 2px rgba(0,0,0,0.05); } .v-table th { background-color: rgba(var(--v-theme-primary), 0.05); color: rgb(var(--v-theme-primary)); font-weight: 600; }"})
        
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
