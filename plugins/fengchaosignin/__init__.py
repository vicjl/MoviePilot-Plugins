import json
import re
import time
from collections import defaultdict
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


class FengchaoSignin(_PluginBase):
    # 插件名称
    plugin_name = "蜂巢签到"
    # 插件描述
    plugin_desc = "蜂巢论坛签到。"
    # 插件图标
    plugin_icon = "https://raw.githubusercontent.com/madrays/MoviePilot-Plugins/main/icons/fengchao.png"
    # 插件版本
    plugin_version = "1.2.9"
    # 插件作者
    plugin_author = "madrays"
    # 作者主页
    author_url = "https://github.com/madrays"
    # 插件配置项ID前缀
    plugin_config_prefix = "fengchaosignin_"
    # 加载顺序
    plugin_order = 24
    # 可使用的用户级别
    auth_level = 2

    # 私有属性
    _enabled = False
    # 任务执行间隔
    _cron = None
    _cookie = None
    _onlyonce = False
    _update_info_now = False
    _notify = False
    _history_days = None
    # 重试相关
    _retry_count = 0  # 最大重试次数
    _current_retry = 0  # 当前重试次数
    _retry_interval = 2  # 重试间隔(小时)
    # MoviePilot数据推送相关
    _mp_push_enabled = False  # 是否启用数据推送
    _mp_push_interval = 1  # 推送间隔(天)
    _last_push_time = None  # 上次推送时间
    # 代理相关
    _use_proxy = True  # 是否使用代理，默认启用
    # 用户名密码
    _username = None
    _password = None

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
            self._update_info_now = config.get("update_info_now", False)
            self._cookie = config.get("cookie", "")
            self._history_days = config.get("history_days", 30)
            self._retry_count = int(config.get("retry_count", 0))
            self._retry_interval = int(config.get("retry_interval", 2))
            self._mp_push_enabled = config.get("mp_push_enabled", False)
            self._mp_push_interval = int(config.get("mp_push_interval", 1))
            self._use_proxy = config.get("use_proxy", True)
            self._username = config.get("username", "")
            self._password = config.get("password", "")
            # 初始化最后推送时间
            self._last_push_time = self.get_data('last_push_time')

        # 重置重试计数
        self._current_retry = 0

        # 停止现有任务
        self.stop_service()

        # 确保scheduler是新的
        self._scheduler = BackgroundScheduler(timezone=settings.TZ)

        # 立即更新个人信息
        if self._update_info_now:
            logger.info("蜂巢插件：立即更新个人信息")
            self._scheduler.add_job(func=self.__update_user_info, trigger='date',
                                    run_date=datetime.now(tz=pytz.timezone(settings.TZ)) + timedelta(seconds=3),
                                    name="蜂巢个人信息更新")
            # 自动关闭开关
            self._update_info_now = False
            self.update_config(self.get_config_dict())

        # 立即运行一次（签到和信息更新）
        if self._onlyonce:
            logger.info(f"蜂巢插件启动，立即运行一次（签到和信息更新）")
            # 安排签到任务
            self._scheduler.add_job(func=self.__signin, trigger='date',
                                    run_date=datetime.now(tz=pytz.timezone(settings.TZ)) + timedelta(seconds=3),
                                    name="蜂巢签到（单次）")
            # 安排信息更新任务
            self._scheduler.add_job(func=self.__update_user_info, trigger='date',
                                    run_date=datetime.now(tz=pytz.timezone(settings.TZ)) + timedelta(seconds=5),
                                    name="蜂巢信息更新（单次）")
            # 关闭一次性开关
            self._onlyonce = False
            self.update_config(self.get_config_dict())

        # 周期运行
        elif self._cron and self._enabled:
            logger.info(f"蜂巢签到服务启动，周期：{self._cron}")
            self._scheduler.add_job(func=self.__signin,
                                    trigger=CronTrigger.from_crontab(self._cron),
                                    name="蜂巢签到")

        # 启动任务
        if self._scheduler.get_jobs():
            self._scheduler.print_jobs()
            self._scheduler.start()

    def get_config_dict(self):
        """获取当前配置字典，用于更新"""
        return {
            "enabled": self._enabled,
            "notify": self._notify,
            "cron": self._cron,
            "onlyonce": self._onlyonce,
            "update_info_now": self._update_info_now,
            "history_days": self._history_days,
            "retry_count": self._retry_count,
            "retry_interval": self._retry_interval,
            "mp_push_enabled": self._mp_push_enabled,
            "mp_push_interval": self._mp_push_interval,
            "use_proxy": self._use_proxy,
            "username": self._username,
            "password": self._password
        }

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
            name=f"蜂巢签到重试 ({self._current_retry}/{self._retry_count})"
        )

        logger.info(f"蜂巢签到失败，将在{retry_interval}小时后重试，当前重试次数: {self._current_retry}/{self._retry_count}")

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
                # 修复：显示固定的重试间隔，而不是累加
                next_retry_hours = self._retry_interval
                retry_info = (
                    f"🔄 重试信息\n"
                    f"• 将在 {next_retry_hours} 小时后进行下一次定时重试\n"
                    f"• 剩余定时重试次数: {remaining_retries}\n"
                    f"━━━━━━━━━━\n"
                )

            self._send_notification(
                title="【❌ 蜂巢签到失败】",
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

    def __update_user_info(self):
        """
        仅更新用户信息，不执行签到
        """
        logger.info("开始执行蜂巢用户信息更新任务...")
        try:
            if not self._username or not self._password:
                raise Exception("未配置用户名和密码")

            proxies = self._get_proxies()
            cookie = self._login_and_get_cookie(proxies)
            if not cookie:
                raise Exception("登录失败，无法获取Cookie")

            # 访问主页以获取userId
            res_main = RequestUtils(cookies=cookie, proxies=proxies, timeout=30).get_res(url="https://pting.club")
            if not res_main or res_main.status_code != 200:
                raise Exception(f"访问主页失败，状态码: {res_main.status_code if res_main else 'N/A'}")

            match = re.search(r'"userId":(\d+)', res_main.text)
            if not match or match.group(1) == "0":
                raise Exception("无法从主页获取有效的用户ID")

            userId = match.group(1)

            # 直接调用Flarum API获取完整的用户信息
            api_url = f"https://pting.club/api/users/{userId}?include=groups,badges,badges.badge.category"
            res_api = RequestUtils(cookies=cookie, proxies=proxies, timeout=30).get_res(url=api_url)

            if not res_api or res_api.status_code != 200:
                raise Exception(f"API请求失败，状态码: {res_api.status_code if res_api else 'N/A'}")

            user_info = res_api.json()
            self.save_data("user_info", user_info)
            logger.info("成功更新并保存了蜂巢用户信息。")

            self._send_notification(
                title="【✅ 蜂巢信息更新成功】",
                text=f"已成功获取并刷新您的蜂巢论坛个人信息。\n"
                     f"🕐 时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
            )

        except Exception as e:
            logger.error(f"更新蜂巢用户信息失败: {e}")
            self._send_notification(
                title="【❌ 蜂巢信息更新失败】",
                text=f"在尝试刷新您的蜂巢论坛个人信息时发生错误。\n"
                     f"💬 原因：{e}\n"
                     f"🕐 时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
            )
        finally:
            # 确保任务执行后，开关状态在配置中被重置
            self._update_info_now = False
            self.update_config(self.get_config_dict())

    def __signin(self, retry_count=0, max_retries=3):
        """
        蜂巢签到
        """
        # 增加任务锁，防止重复执行
        if hasattr(self, '_signing_in') and self._signing_in:
            logger.info("已有签到任务在执行，跳过当前任务")
            return

        self._signing_in = True
        attempt = 0
        try:
            # 检查用户名密码是否配置
            if not self._username or not self._password:
                logger.error("未配置用户名密码，无法进行签到")
                if self._notify:
                    self._send_notification(
                        title="【❌ 蜂巢签到失败】",
                        text=(
                            f"📢 执行结果\n"
                            f"━━━━━━━━━━\n"
                            f"🕐 时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
                            f"❌ 状态：签到失败，未配置用户名密码\n"
                            f"━━━━━━━━━━\n"
                            f"💡 配置方法\n"
                            f"• 在插件设置中填写蜂巢论坛用户名和密码\n"
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

                # 每次都重新登录获取cookie
                logger.info(f"开始登录蜂巢论坛获取cookie...")
                cookie = self._login_and_get_cookie(proxies)
                if not cookie:
                    logger.error(f"登录失败，无法获取cookie")
                    if attempt < max_retries:
                        continue
                    raise Exception("登录失败，无法获取cookie")

                logger.info(f"登录成功，成功获取cookie")

                # 使用获取的cookie访问蜂巢
                try:
                    res = RequestUtils(cookies=cookie, proxies=proxies, timeout=30).get_res(url="https://pting.club")
                except Exception as e:
                    logger.error(f"请求蜂巢出错: {str(e)}")
                    if attempt < max_retries:
                        continue
                    raise Exception("连接站点出错")

                if not res or res.status_code != 200:
                    logger.error(f"请求蜂巢返回错误状态码: {res.status_code if res else '无响应'}")
                    if attempt < max_retries:
                        continue
                    raise Exception("无法连接到站点")

                # 获取csrfToken
                pattern = r'"csrfToken":"(.*?)"'
                csrfToken = re.findall(pattern, res.text)
                if not csrfToken:
                    logger.error("请求csrfToken失败")
                    if attempt < max_retries:
                        continue
                    raise Exception("无法获取CSRF令牌")

                csrfToken = csrfToken[0]
                logger.info(f"获取csrfToken成功 {csrfToken}")

                # 获取userid
                pattern = r'"userId":(\d+)'
                match = re.search(pattern, res.text)

                if match and match.group(1) != "0":
                    userId = match.group(1)
                    logger.info(f"获取userid成功 {userId}")

                    # 如果开启了蜂巢论坛PT人生数据更新，尝试更新数据
                    if self._mp_push_enabled:
                        self.__push_mp_stats(user_id=userId, csrf_token=csrfToken, cookie=cookie)
                else:
                    logger.error("未找到userId")
                    if attempt < max_retries:
                        continue
                    raise Exception("无法获取用户ID")

                # 准备签到请求
                headers = {
                    "X-Csrf-Token": csrfToken,
                    "X-Http-Method-Override": "PATCH",
                    "Cookie": cookie
                }

                data = {
                    "data": {
                        "type": "users",
                        "attributes": {
                            "canCheckin": False,
                            "totalContinuousCheckIn": 2
                        },
                        "id": userId
                    }
                }

                # 开始签到
                try:
                    res = RequestUtils(headers=headers, proxies=proxies, timeout=30).post_res(
                        url=f"https://pting.club/api/users/{userId}",
                        json=data
                    )
                except Exception as e:
                    logger.error(f"签到请求出错: {str(e)}")
                    if attempt < max_retries:
                        continue
                    raise Exception("签到请求异常")

                if not res or res.status_code != 200:
                    logger.error(f"蜂巢签到失败，状态码: {res.status_code if res else '无响应'}")
                    if attempt < max_retries:
                        continue
                    raise Exception("API请求错误")

                # 签到成功
                sign_dict = json.loads(res.text)

                # 直接保存签到后的用户信息，不再进行二次请求
                self.save_data("user_info", sign_dict)
                logger.info("成功获取并保存用户信息。")

                money = sign_dict['data']['attributes']['money']
                totalContinuousCheckIn = sign_dict['data']['attributes']['totalContinuousCheckIn']
                # 获取签到奖励花粉数量
                lastCheckinMoney = sign_dict['data']['attributes'].get('lastCheckinMoney', 0)

                # 检查是否已签到
                if "canCheckin" in sign_dict['data']['attributes'] and not sign_dict['data']['attributes']['canCheckin']:
                    status_text = "已签到"
                    reward_text = "今日已领取奖励"
                    logger.info(f"蜂巢已签到，当前花粉: {money}，累计签到: {totalContinuousCheckIn}")
                else:
                    status_text = "签到成功"
                    reward_text = f"获得{lastCheckinMoney}花粉奖励"
                    logger.info(f"蜂巢签到成功，获得{lastCheckinMoney}花粉，当前花粉: {money}，累计签到: {totalContinuousCheckIn}")

                # 发送通知
                if self._notify:
                    self._send_notification(
                        title=f"【✅ 蜂巢{status_text}】",
                        text=(
                            f"📢 执行结果\n"
                            f"━━━━━━━━━━\n"
                            f"🕐 时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
                            f"✨ 状态：{status_text}\n"
                            f"🎁 奖励：{reward_text}\n"
                            f"━━━━━━━━━━\n"
                            f"📊 积分统计\n"
                            f"🌸 花粉：{money}\n"
                            f"📆 签到天数：{totalContinuousCheckIn}\n"
                            f"━━━━━━━━━━"
                        )
                    )

                # 读取历史记录
                history = {
                    "date": datetime.today().strftime('%Y-%m-%d %H:%M:%S'),
                    "status": status_text,
                    "money": money,
                    "totalContinuousCheckIn": totalContinuousCheckIn,
                    "lastCheckinMoney": lastCheckinMoney,
                    "failure_count": attempt
                }

                # 保存签到历史
                self._save_history(history)

                # 如果是重试后成功，重置重试计数
                if self._current_retry > 0:
                    logger.info(f"蜂巢签到重试成功，重置重试计数")
                    self._current_retry = 0

                # 签到成功，退出循环
                return True

        except Exception as e:
            logger.error(f"签到过程发生异常: {str(e)}")
            import traceback
            logger.error(f"错误详情: {traceback.format_exc()}")

            # 保存失败记录
            failure_history_record = {
                "date": datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                "status": "签到失败",
                "reason": str(e),
                "failure_count": attempt + 1
            }
            self._save_history(failure_history_record)

            # 所有重试失败，发送通知并退出
            self._send_signin_failure_notification(str(e), attempt)

            # 设置下次定时重试
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
            # 释放锁
            self._signing_in = False

    def _save_history(self, record):
        """
        保存签到历史记录
        """
        # 读取历史记录
        history = self.get_data('history') or []

        # 如果是失败状态，添加重试信息
        if "失败" in record.get("status", ""):
            record["retry"] = {
                "enabled": self._retry_count > 0,
                "current": self._current_retry,
                "max": self._retry_count,
                "interval": self._retry_interval
            }

        # 添加新记录
        history.append(record)

        # 保留指定天数的记录
        if self._history_days:
            try:
                thirty_days_ago = time.time() - int(self._history_days) * 24 * 60 * 60
                history = [record for record in history if
                           datetime.strptime(record["date"],
                                             '%Y-%m-%d %H:%M:%S').timestamp() >= thirty_days_ago]
            except Exception as e:
                logger.error(f"清理历史记录异常: {str(e)}")

        # 保存历史记录
        self.save_data(key="history", value=history)

    def get_state(self) -> bool:
        return self._enabled

    @staticmethod
    def get_command() -> List[Dict[str, Any]]:
        pass

    def get_api(self) -> List[Dict[str, Any]]:
        pass

    def get_service(self) -> List[Dict[str, Any]]:
        """
        注册插件公共服务
        """
        services = []

        if self._enabled and self._cron:
            services.append({
                "id": "FengchaoSignin",
                "name": "蜂巢签到服务",
                "trigger": CronTrigger.from_crontab(self._cron),
                "func": self.__signin,
                "kwargs": {}
            })

        if self._enabled and self._mp_push_enabled:
            services.append({
                "id": "MoviePilotStatsPush",
                "name": "蜂巢论坛PT人生数据更新服务",
                "trigger": "interval",
                "func": self.__check_and_push_mp_stats,
                "kwargs": {"hours": 6}
            })

        return services

    def get_form(self) -> Tuple[List[dict], Dict[str, Any]]:
        """
        拼装插件配置页面
        """
        return [
            {
                'component': 'VForm',
                'content': [
                    {
                        'component': 'VCard',
                        'props': {
                            'variant': 'outlined',
                            'class': 'mt-3'
                        },
                        'content': [
                            {
                                'component': 'VCardTitle',
                                'props': {
                                    'class': 'd-flex align-center'
                                },
                                'content': [
                                    {
                                        'component': 'VIcon',
                                        'props': {
                                            'style': 'color: #1976D2;',
                                            'class': 'mr-2'
                                        },
                                        'text': 'mdi-calendar-check'
                                    },
                                    {
                                        'component': 'span',
                                        'text': '蜂巢签到设置'
                                    }
                                ]
                            },
                            {
                                'component': 'VDivider'
                            },
                            {
                                'component': 'VCardText',
                                'content': [
                                    {
                                        'component': 'VRow',
                                        'content': [
                                            {
                                                'component': 'VCol',
                                                'props': {'cols': 12, 'md': 4},
                                                'content': [
                                                    {'component': 'VSwitch',
                                                     'props': {'model': 'enabled', 'label': '启用插件'}}
                                                ]
                                            },
                                            {
                                                'component': 'VCol',
                                                'props': {'cols': 12, 'md': 4},
                                                'content': [
                                                    {'component': 'VSwitch',
                                                     'props': {'model': 'notify', 'label': '开启通知'}}
                                                ]
                                            },
                                        ]
                                    },
                                    {
                                        'component': 'VRow',
                                        'content': [
                                            {
                                                'component': 'VCol',
                                                'props': {'cols': 12, 'md': 4},
                                                'content': [
                                                    {
                                                        'component': 'VSwitch',
                                                        'props': {
                                                            'model': 'onlyonce',
                                                            'label': '立即运行一次',
                                                            'hint': '同时执行签到和信息更新任务'
                                                        }
                                                    }
                                                ]
                                            },
                                            {
                                                'component': 'VCol',
                                                'props': {'cols': 12, 'md': 4},
                                                'content': [
                                                    {
                                                        'component': 'VSwitch',
                                                        'props': {
                                                            'model': 'update_info_now',
                                                            'label': '立即更新个人信息',
                                                            'hint': '不执行签到，仅刷新插件页面显示的用户信息'
                                                        }
                                                    }
                                                ]
                                            }
                                        ]
                                    },
                                    {
                                        'component': 'VRow',
                                        'content': [
                                            {
                                                'component': 'VCol',
                                                'props': {'cols': 12, 'md': 6},
                                                'content': [
                                                    {
                                                        'component': 'VTextField',
                                                        'props': {
                                                            'model': 'username', 'label': '用户名',
                                                            'placeholder': '蜂巢论坛用户名',
                                                            'hint': 'Cookie失效时自动登录获取新Cookie'
                                                        }
                                                    }
                                                ]
                                            },
                                            {
                                                'component': 'VCol',
                                                'props': {'cols': 12, 'md': 6},
                                                'content': [
                                                    {
                                                        'component': 'VTextField',
                                                        'props': {
                                                            'model': 'password', 'label': '密码',
                                                            'placeholder': '蜂巢论坛密码', 'type': 'password',
                                                            'hint': 'Cookie失效时自动登录获取新Cookie'
                                                        }
                                                    }
                                                ]
                                            }
                                        ]
                                    },
                                    {
                                        'component': 'VRow',
                                        'content': [
                                            {
                                                'component': 'VCol',
                                                'props': {'cols': 12, 'md': 6},
                                                'content': [
                                                    {
                                                        'component': 'VCronField',
                                                        'props': {
                                                            'model': 'cron', 'label': '签到周期',
                                                            'placeholder': '30 8 * * *',
                                                            'hint': '五位cron表达式，每天早上8:30执行'
                                                        }
                                                    }
                                                ]
                                            },
                                            {
                                                'component': 'VCol',
                                                'props': {'cols': 12, 'md': 6},
                                                'content': [
                                                    {
                                                        'component': 'VTextField',
                                                        'props': {
                                                            'model': 'history_days', 'label': '历史保留天数',
                                                            'placeholder': '30', 'hint': '历史记录保留天数'
                                                        }
                                                    }
                                                ]
                                            }
                                        ]
                                    },
                                    {
                                        'component': 'VRow',
                                        'content': [
                                            {
                                                'component': 'VCol',
                                                'props': {'cols': 12, 'md': 6},
                                                'content': [
                                                    {
                                                        'component': 'VTextField',
                                                        'props': {
                                                            'model': 'retry_count', 'label': '失败重试次数',
                                                            'type': 'number', 'placeholder': '0',
                                                            'hint': '0表示不重试，大于0则在签到失败后重试'
                                                        }
                                                    }
                                                ]
                                            },
                                            {
                                                'component': 'VCol',
                                                'props': {'cols': 12, 'md': 6},
                                                'content': [
                                                    {
                                                        'component': 'VTextField',
                                                        'props': {
                                                            'model': 'retry_interval', 'label': '重试间隔(小时)',
                                                            'type': 'number', 'placeholder': '2',
                                                            'hint': '签到失败后多少小时后重试'
                                                        }
                                                    }
                                                ]
                                            }
                                        ]
                                    },
                                    {
                                        'component': 'VRow',
                                        'content': [
                                            {
                                                'component': 'VCol',
                                                'props': {'cols': 12, 'md': 6},
                                                'content': [
                                                    {
                                                        'component': 'VSwitch',
                                                        'props': {
                                                            'model': 'use_proxy', 'label': '使用代理',
                                                            'hint': '与蜂巢论坛通信时使用系统代理'
                                                        }
                                                    }
                                                ]
                                            }
                                        ]
                                    },
                                    {'component': 'VDivider', 'props': {'class': 'my-3'}},
                                    {
                                        'component': 'VRow',
                                        'content': [
                                            {
                                                'component': 'VCol',
                                                'props': {'cols': 12},
                                                'content': [
                                                    {
                                                        'component': 'div',
                                                        'props': {'class': 'd-flex align-center mb-3'},
                                                        'content': [
                                                            {
                                                                'component': 'VIcon',
                                                                'props': {'style': 'color: #1976D2;', 'class': 'mr-2'},
                                                                'text': 'mdi-chart-box'
                                                            },
                                                            {
                                                                'component': 'span',
                                                                'props': {'style': 'font-size: 1.1rem; font-weight: 500;'},
                                                                'text': '蜂巢个人主页PT人生卡片数据更新'
                                                            }
                                                        ]
                                                    }
                                                ]
                                            }
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
                                                        'component': 'VSwitch',
                                                        'props': {
                                                            'model': 'mp_push_enabled',
                                                            'label': '启用PT人生数据更新',
                                                            'hint': '每次签到时都会自动更新PT人生数据'
                                                        }
                                                    }
                                                ]
                                            }
                                        ]
                                    }
                                ]
                            }
                        ]
                    }
                ]
            }
        ], {
            "enabled": False, "notify": True, "cron": "30 8 * * *", "onlyonce": False, "update_info_now": False,
            "cookie": "", "username": "", "password": "", "history_days": 30, "retry_count": 0, "retry_interval": 2,
            "mp_push_enabled": False, "mp_push_interval": 1, "use_proxy": True
        }

    def _map_fa_to_mdi(self, icon_class: str) -> str:
        """
        Maps common Font Awesome icon names to MDI icon names.
        """
        if not icon_class or not isinstance(icon_class, str):
            return 'mdi-account-group'
        if icon_class.startswith('mdi-'):
            return icon_class

        mapping = {
            'fa-user-tie': 'mdi-account-tie', 'fa-crown': 'mdi-crown', 'fa-shield-alt': 'mdi-shield-outline',
            'fa-user-shield': 'mdi-account-shield', 'fa-user-cog': 'mdi-account-cog',
            'fa-user-check': 'mdi-account-check', 'fa-fan': 'mdi-fan', 'fa-user': 'mdi-account',
            'fa-users': 'mdi-account-group', 'fa-cogs': 'mdi-cog', 'fa-cog': 'mdi-cog', 'fa-star': 'mdi-star',
            'fa-gem': 'mdi-diamond'
        }
        match = re.search(r'fa-[\w-]+', icon_class)
        if match:
            core_icon = match.group(0)
            return mapping.get(core_icon, 'mdi-account-group')
        return 'mdi-account-group'

    def _format_pollen(self, value: Any) -> str:
        """
        Formats the pollen value.
        """
        if value is None:
            return '—'
        try:
            num = float(value)
            if num == int(num):
                return str(int(num))
            else:
                return f'{round(num, 3):g}'
        except (ValueError, TypeError):
            return str(value)

    def get_page(self) -> List[dict]:
        """
        构建插件详情页面
        """
        history = self.get_data('history') or []
        user_info = self.get_data('user_info')
        user_info_card = None

        frost_style = 'background-color: rgba(255, 255, 255, 0.75); backdrop-filter: blur(5px); -webkit-backdrop-filter: blur(5px); border: 1px solid rgba(255, 255, 255, 0.2); border-radius: 8px;'

        if user_info and 'data' in user_info and 'attributes' in user_info['data']:
            user_attrs = user_info['data']['attributes']
            username = user_attrs.get('displayName', '未知用户')
            avatar_url = user_attrs.get('avatarUrl', '')
            money = self._format_pollen(user_attrs.get('money', 0))
            discussion_count = user_attrs.get('discussionCount', 0)
            comment_count = user_attrs.get('commentCount', 0)
            follower_count = user_attrs.get('followerCount', 0)
            following_count = user_attrs.get('followingCount', 0)
            last_checkin_time = user_attrs.get('lastCheckinTime', '未知')
            total_continuous_checkin = user_attrs.get('totalContinuousCheckIn', 0)
            join_time_str = user_attrs.get('joinTime', '')
            last_seen_at_str = user_attrs.get('lastSeenAt', '')
            background_image = user_attrs.get('decorationProfileBackground') or user_attrs.get('cover')

            try:
                join_time = datetime.fromisoformat(join_time_str.replace('Z', '+00:00')).strftime('%Y-%m-%d')
            except:
                join_time = '未知'
            try:
                last_seen_at = datetime.fromisoformat(last_seen_at_str.replace('Z', '+00:00')).strftime(
                    '%Y-%m-%d %H:%M')
            except:
                last_seen_at = '未知'

            groups = []
            if 'included' in user_info:
                for item in user_info.get('included', []):
                    if item.get('type') == 'groups':
                        groups.append({
                            'name': item.get('attributes', {}).get('nameSingular', ''),
                            'color': item.get('attributes', {}).get('color', '#888'),
                            'icon': self._map_fa_to_mdi(item.get('attributes', {}).get('icon', ''))
                        })

            badges = []
            user_badges_data = user_attrs.get('badges', [])
            for badge_item in user_badges_data:
                core_badge_info = badge_item.get('badge', {})
                if not core_badge_info: continue
                category_info = core_badge_info.get('category', {})
                badges.append({
                    'name': core_badge_info.get('name', '未知徽章'),
                    'icon': core_badge_info.get('icon', 'fas fa-award'),
                    'description': core_badge_info.get('description', '无描述'),
                    'image': core_badge_info.get('image'),
                    'category': category_info.get('name', '其他')
                })
            badge_count = len(badges)

            categorized_badges = defaultdict(list)
            for badge in badges:
                categorized_badges[badge.get('category', '其他')].append(badge)
            
            # This will contain a single div wrapper for all categories
            badge_category_components = []
            if categorized_badges:
                all_category_cards = []
                for category_name, badge_list in sorted(categorized_badges.items()):
                    badge_items_with_dividers = []
                    for i, badge in enumerate(badge_list):
                        badge_items_with_dividers.append({
                            'component': 'div',
                            'props': {
                                'class': 'ma-1 pa-1 d-flex flex-column align-center',
                                'style': 'width: 90px; text-align: center;',
                                'title': badge.get('description', '无描述')
                            },
                            'content': [
                                {
                                    'component': 'VImg' if badge.get('image') else 'VIcon',
                                    'props': ({
                                        'src': badge.get('image'), 'height': '48', 'width': '48', 'class': 'mb-1'
                                    } if badge.get('image') else {
                                        'icon': self._map_fa_to_mdi(badge.get('icon')), 'size': '48', 'class': 'mb-1'
                                    })
                                },
                                {
                                    'component': 'div',
                                    'props': {'class': 'marquee-text-wrapper', 'style': 'width: 100%; height: 20px;'},
                                    'content': [{'component': 'div', 'props': {'class': 'text-caption marquee-text', 'style': 'line-height: 20px; font-weight: 500;'}, 'text': badge.get('name', '未知徽章')}]
                                }
                            ]
                        })
                        if i < len(badge_list) - 1:
                            badge_items_with_dividers.append({
                                'component': 'VDivider',
                                'props': {'vertical': True, 'class': 'my-2'}
                            })
                    
                    all_category_cards.append({
                        'component': 'div',
                        'props': {'class': 'ma-1 pa-2', 'style': f'{frost_style} border-radius: 12px;'},
                        'content': [
                            {'component': 'div', 'props': {'class': 'text-subtitle-2 grey--text text--darken-1', 'style': 'text-align: center;'}, 'text': category_name},
                            {'component': 'VDivider', 'props': {'class': 'my-1'}},
                            {'component': 'div', 'props': {'class': 'd-flex flex-wrap justify-center align-center'}, 'content': badge_items_with_dividers}
                        ]
                    })
                
                badge_category_components.append({
                    'component': 'div',
                    'props': {'class': 'd-flex flex-wrap'},
                    'content': all_category_cards
                })


            user_info_card = {
                'component': 'VCard',
                'props': {'variant': 'outlined', 'class': 'mb-4', 'style': f"background-image: url('{background_image}'); background-size: cover; background-position: center;" if background_image else ''},
                'content': [
                    {'component': 'VDivider'},
                    {'component': 'VCardText', 'content': [
                        {'component': 'VRow', 'props': {'class': 'ma-1'}, 'content': [
                            {'component': 'VCol', 'props': {'cols': 12, 'md': 5}, 'content': [
                                {'component': 'div', 'props': {'class': 'd-flex align-center'}, 'content': [
                                    {'component': 'div', 'props': {'class': 'mr-3', 'style': 'position: relative; width: 90px; height: 90px;'}, 'content': [
                                        {'component': 'VAvatar', 'props': {'size': 60, 'rounded': 'circle', 'style': 'position: absolute; top: 15px; left: 15px; z-index: 1;'}, 'content': [{'component': 'VImg', 'props': {'src': avatar_url, 'alt': username}}]},
                                        {'component': 'div', 'props': {'style': f"position: absolute; top: 0; left: 0; width: 90px; height: 90px; background-image: url('{user_attrs.get('decorationAvatarFrame', '')}'); background-size: contain; background-repeat: no-repeat; background-position: center; z-index: 2;"}} if user_attrs.get('decorationAvatarFrame') else {}
                                    ]},
                                    {'component': 'div', 'content': [
                                        {'component': 'div', 'props': {'class': 'text-h6 mb-1 pa-2 d-inline-block elevation-2', 'style': frost_style}, 'text': username},
                                        {'component': 'div', 'props': {'class': 'd-flex flex-wrap mt-1'}, 'content': [
                                            {'component': 'VChip', 'props': {'style': f"background-color: {group.get('color', '#6B7CA8')}; color: white;", 'size': 'small', 'class': 'mr-1 mb-1', 'variant': 'elevated'}, 'content': [
                                                {'component': 'VIcon', 'props': {'start': True, 'size': 'small'}, 'text': group.get('icon')},
                                                {'component': 'span', 'text': group.get('name')}
                                            ]} for group in groups
                                        ]}
                                    ]}
                                ]},
                                {'component': 'VRow', 'props': {'class': 'mt-2'}, 'content': [
                                    {'component': 'VCol', 'props': {'cols': 12}, 'content': [
                                        {'component': 'div', 'props': {'class': 'pa-1 elevation-2 mb-1', 'style': f'{frost_style} width: fit-content;'}, 'content': [{'component': 'div', 'props': {'class': 'd-flex align-center text-caption'}, 'content': [
                                            {'component': 'VIcon', 'props': {'style': 'color: #4CAF50;', 'size': 'x-small', 'class': 'mr-1'}, 'text': 'mdi-calendar'},
                                            {'component': 'span', 'text': f'注册于 {join_time}'}
                                        ]}]},
                                        {'component': 'div', 'props': {'class': 'pa-1 elevation-2 mb-1', 'style': f'{frost_style} width: fit-content;'}, 'content': [{'component': 'div', 'props': {'class': 'd-flex align-center text-caption'}, 'content': [
                                            {'component': 'VIcon', 'props': {'style': 'color: #2196F3;', 'size': 'x-small', 'class': 'mr-1'}, 'text': 'mdi-clock-outline'},
                                            {'component': 'span', 'text': f'最后访问 {last_seen_at}'}
                                        ]}]},
                                        {'component': 'div', 'props': {'class': 'pa-1 elevation-2', 'style': f'{frost_style} width: fit-content;'}, 'content': [{'component': 'div', 'props': {'class': 'd-flex align-center text-caption'}, 'content': [
                                            {'component': 'VIcon', 'props': {'style': 'color: #E64A19;', 'size': 'x-small', 'class': 'mr-1'}, 'text': 'mdi-medal-outline'},
                                            {'component': 'span', 'text': f'拥有 {badge_count} 枚徽章'}
                                        ]}]}
                                    ]}
                                ]}
                            ]},
                            {'component': 'VCol', 'props': {'cols': 12, 'md': 7}, 'content': [
                                {'component': 'VRow', 'content': [
                                    {'component': 'VCol', 'props': {'cols': 6, 'md': 4}, 'content': [{'component': 'div', 'props': {'class': 'text-center pa-2 elevation-2', 'style': frost_style}, 'content': [
                                        {'component': 'div', 'props': {'class': 'd-flex justify-center align-center'}, 'content': [
                                            {'component': 'VIcon', 'props': {'style': 'color: #FFC107;', 'class': 'mr-1'}, 'text': 'mdi-flower'},
                                            {'component': 'span', 'props': {'class': 'text-h6'}, 'text': str(money)}
                                        ]},
                                        {'component': 'div', 'props': {'class': 'text-caption mt-1'}, 'text': '花粉'}
                                    ]}]},
                                    {'component': 'VCol', 'props': {'cols': 6, 'md': 4}, 'content': [{'component': 'div', 'props': {'class': 'text-center pa-2 elevation-2', 'style': frost_style}, 'content': [
                                        {'component': 'div', 'props': {'class': 'd-flex justify-center align-center'}, 'content': [
                                            {'component': 'VIcon', 'props': {'style': 'color: #3F51B5;', 'class': 'mr-1'}, 'text': 'mdi-forum'},
                                            {'component': 'span', 'props': {'class': 'text-h6'}, 'text': str(discussion_count)}
                                        ]},
                                        {'component': 'div', 'props': {'class': 'text-caption mt-1'}, 'text': '主题'}
                                    ]}]},
                                    {'component': 'VCol', 'props': {'cols': 6, 'md': 4}, 'content': [{'component': 'div', 'props': {'class': 'text-center pa-2 elevation-2', 'style': frost_style}, 'content': [
                                        {'component': 'div', 'props': {'class': 'd-flex justify-center align-center'}, 'content': [
                                            {'component': 'VIcon', 'props': {'style': 'color: #00BCD4;', 'class': 'mr-1'}, 'text': 'mdi-comment-text-multiple'},
                                            {'component': 'span', 'props': {'class': 'text-h6'}, 'text': str(comment_count)}
                                        ]},
                                        {'component': 'div', 'props': {'class': 'text-caption mt-1'}, 'text': '评论'}
                                    ]}]},
                                    {'component': 'VCol', 'props': {'cols': 6, 'md': 4}, 'content': [{'component': 'div', 'props': {'class': 'text-center pa-2 elevation-2', 'style': frost_style}, 'content': [
                                        {'component': 'div', 'props': {'class': 'd-flex justify-center align-center'}, 'content': [
                                            {'component': 'VIcon', 'props': {'style': 'color: #673AB7;', 'class': 'mr-1'}, 'text': 'mdi-account-group'},
                                            {'component': 'span', 'props': {'class': 'text-h6'}, 'text': str(follower_count)}
                                        ]},
                                        {'component': 'div', 'props': {'class': 'text-caption mt-1'}, 'text': '粉丝'}
                                    ]}]},
                                    {'component': 'VCol', 'props': {'cols': 6, 'md': 4}, 'content': [{'component': 'div', 'props': {'class': 'text-center pa-2 elevation-2', 'style': frost_style}, 'content': [
                                        {'component': 'div', 'props': {'class': 'd-flex justify-center align-center'}, 'content': [
                                            {'component': 'VIcon', 'props': {'style': 'color: #03A9F4;', 'class': 'mr-1'}, 'text': 'mdi-account-multiple-plus'},
                                            {'component': 'span', 'props': {'class': 'text-h6'}, 'text': str(following_count)}
                                        ]},
                                        {'component': 'div', 'props': {'class': 'text-caption mt-1'}, 'text': '关注'}
                                    ]}]},
                                    {'component': 'VCol', 'props': {'cols': 6, 'md': 4}, 'content': [{'component': 'div', 'props': {'class': 'text-center pa-2 elevation-2', 'style': frost_style}, 'content': [
                                        {'component': 'div', 'props': {'class': 'd-flex justify-center align-center'}, 'content': [
                                            {'component': 'VIcon', 'props': {'style': 'color: #009688;', 'class': 'mr-1'}, 'text': 'mdi-calendar-check'},
                                            {'component': 'span', 'props': {'class': 'text-h6'}, 'text': str(total_continuous_checkin)}
                                        ]},
                                        {'component': 'div', 'props': {'class': 'text-caption mt-1'}, 'text': '连续签到'}
                                    ]}]}
                                ]}
                            ]}
                        ]},
                        *badge_category_components,
                        {'component': 'div', 'props': {'class': 'mt-2 text-caption text-right pa-1 elevation-2 d-inline-block float-right', 'style': frost_style}, 'text': f'最后签到: {last_checkin_time}'}
                    ]}
                ]
            }

        if not history:
            components = []
            if user_info_card:
                components.append(user_info_card)
            components.extend([
                {'component': 'VAlert', 'props': {'type': 'info', 'variant': 'tonal', 'text': '暂无签到记录，请先配置用户名和密码并启用插件', 'class': 'mb-2', 'prepend-icon': 'mdi-information'}},
                {'component': 'VCard', 'props': {'variant': 'outlined', 'class': 'mb-4'}, 'content': [
                    {'component': 'VCardTitle', 'props': {'class': 'd-flex align-center'}, 'content': [
                        {'component': 'VIcon', 'props': {'color': 'amber-darken-2', 'class': 'mr-2'}, 'text': 'mdi-flower'},
                        {'component': 'span', 'props': {'class': 'text-h6'}, 'text': '签到奖励说明'}
                    ]},
                    {'component': 'VDivider'},
                    {'component': 'VCardText', 'props': {'class': 'pa-3'}, 'content': [
                        {'component': 'div', 'props': {'class': 'd-flex align-center mb-2'}, 'content': [
                            {'component': 'VIcon', 'props': {'style': 'color: #FF8F00;', 'size': 'small', 'class': 'mr-2'}, 'text': 'mdi-check-circle'},
                            {'component': 'span', 'text': '每日签到可获得随机花粉奖励'}
                        ]},
                        {'component': 'div', 'props': {'class': 'd-flex align-center'}, 'content': [
                            {'component': 'VIcon', 'props': {'style': 'color: #1976D2;', 'size': 'small', 'class': 'mr-2'}, 'text': 'mdi-calendar-check'},
                            {'component': 'span', 'text': '连续签到可累积天数，提升论坛等级'}
                        ]}
                    ]}
                ]}
            ])
            return components

        history = sorted(history, key=lambda x: x.get("date", ""), reverse=True)
        history_rows = []
        status_colors = {"签到成功": "#4CAF50", "已签到": "#2196F3", "签到失败": "#F44336"}
        status_icons = {"签到成功": "mdi-check-circle", "已签到": "mdi-information-outline", "签到失败": "mdi-close-circle"}
        
        for record in history:
            status_text = record.get("status", "未知")
            status_color = status_colors.get(status_text, "#9E9E9E")
            status_icon = status_icons.get(status_text, "mdi-help-circle")
            money_text = self._format_pollen(record.get('money'))

            history_rows.append({
                'component': 'tr',
                'content': [
                    {'component': 'td', 'props': {'class': 'text-caption'}, 'text': record.get("date", "")},
                    {'component': 'td', 'content': [
                        {'component': 'VChip', 'props': {'style': f'background-color: {status_color}; color: white;', 'size': 'small', 'variant': 'elevated'}, 'content': [
                            {'component': 'VIcon', 'props': {'start': True, 'style': 'color: white;', 'size': 'small'}, 'text': status_icon},
                            {'component': 'span', 'text': status_text}
                        ]},
                        {'component': 'div', 'props': {'class': 'mt-1 text-caption grey--text'}, 'text': f"将在{record.get('retry', {}).get('interval', self._retry_interval)}小时后重试 ({record.get('retry', {}).get('current', 0)}/{record.get('retry', {}).get('max', self._retry_count)})" if status_text == "签到失败" and record.get('retry', {}).get('enabled', False) and record.get('retry', {}).get('current', 0) > 0 else ""}
                    ]},
                    {'component': 'td', 'text': str(record.get('failure_count', '—'))},
                    {'component': 'td', 'content': [{'component': 'div', 'props': {'class': 'd-flex align-center'}, 'content': [
                        {'component': 'VIcon', 'props': {'style': 'color: #FFC107;', 'class': 'mr-1'}, 'text': 'mdi-flower'},
                        {'component': 'span', 'text': money_text}
                    ]}]},
                    {'component': 'td', 'content': [{'component': 'div', 'props': {'class': 'd-flex align-center'}, 'content': [
                        {'component': 'VIcon', 'props': {'style': 'color: #1976D2;', 'class': 'mr-1'}, 'text': 'mdi-calendar-check'},
                        {'component': 'span', 'text': record.get('totalContinuousCheckIn', '—')}
                    ]}]},
                    {'component': 'td', 'content': [{'component': 'div', 'props': {'class': 'd-flex align-center'}, 'content': [
                        {'component': 'VIcon', 'props': {'style': 'color: #FF8F00;', 'class': 'mr-1'}, 'text': 'mdi-gift'},
                        {'component': 'span', 'text': f"{self._format_pollen(record.get('lastCheckinMoney', 0))}花粉" if ("签到成功" in status_text or "已签到" in status_text) and record.get('lastCheckinMoney', 0) > 0 else '—'}
                    ]}]}
                ]
            })

        components = []
        if user_info_card:
            components.append(user_info_card)
        components.append({
            'component': 'VCard', 'props': {'variant': 'outlined', 'class': 'mb-4'}, 'content': [
                {'component': 'VCardTitle', 'props': {'class': 'd-flex align-center'}, 'content': [
                    {'component': 'VIcon', 'props': {'style': 'color: #9C27B0;', 'class': 'mr-2'}, 'text': 'mdi-calendar-check'},
                    {'component': 'span', 'props': {'class': 'text-h6 font-weight-bold'}, 'text': '蜂巢签到历史'},
                    {'component': 'VSpacer'},
                    {'component': 'VChip', 'props': {'style': 'background-color: #FF9800; color: white;', 'size': 'small', 'variant': 'elevated'}, 'content': [
                        {'component': 'VIcon', 'props': {'start': True, 'style': 'color: white;', 'size': 'small'}, 'text': 'mdi-flower'},
                        {'component': 'span', 'text': '每日可得花粉奖励'}
                    ]}
                ]},
                {'component': 'VDivider'},
                {'component': 'VCardText', 'props': {'class': 'pa-2'}, 'content': [
                    {'component': 'VTable', 'props': {'hover': True, 'density': 'comfortable'}, 'content': [
                        {'component': 'thead', 'content': [{'component': 'tr', 'content': [
                            {'component': 'th', 'text': '时间'}, {'component': 'th', 'text': '状态'}, {'component': 'th', 'text': '失败次数'},
                            {'component': 'th', 'text': '花粉'}, {'component': 'th', 'text': '签到天数'}, {'component': 'th', 'text': '奖励'}
                        ]}]},
                        {'component': 'tbody', 'content': history_rows}
                    ]}
                ]}
            ]
        })
        components.append({
            'component': 'style',
            'text': """
                .v-table { border-radius: 8px; overflow: hidden; box-shadow: 0 1px 2px rgba(0,0,0,0.05); }
                .v-table th { background-color: rgba(var(--v-theme-primary), 0.05); color: rgb(var(--v-theme-primary)); font-weight: 600; }
                .marquee-text-wrapper { display: flex; justify-content: center; align-items: center; overflow: hidden; }
                .marquee-text { white-space: nowrap; overflow: hidden; text-overflow: ellipsis; max-width: 100%; text-align: center; }
                .marquee-text-wrapper:hover .marquee-text {
                    max-width: none;
                    overflow: visible;
                    text-overflow: clip;
                    animation: slide-to-reveal 5s ease-in-out;
                }
                @keyframes slide-to-reveal {
                    0% { transform: translateX(0); }
                    40%, 60% { transform: translateX(calc(90px - 100% - 16px)); }
                    100% { transform: translateX(0); }
                }
                """
        })
        return components

    def stop_service(self):
        """
        退出插件
        """
        try:
            if self._scheduler:
                self._scheduler.remove_all_jobs()
                if self._scheduler.running:
                    self._scheduler.shutdown()
                self._scheduler = None
        except Exception as e:
            logger.error("退出插件失败：%s" % str(e))

    def __check_and_push_mp_stats(self):
        """检查是否需要更新蜂巢论坛PT人生数据"""
        if hasattr(self, '_pushing_stats') and self._pushing_stats:
            logger.info("已有更新PT人生数据任务在执行，跳过当前任务")
            return
        self._pushing_stats = True
        try:
            if not self._mp_push_enabled: return
            if not self._username or not self._password:
                logger.error("未配置用户名密码，无法更新PT人生数据")
                return
            proxies = self._get_proxies()
            now = datetime.now()
            if self._last_push_time:
                last_push = datetime.strptime(self._last_push_time, '%Y-%m-%d %H:%M:%S')
                if (now - last_push).days < self._mp_push_interval:
                    logger.info(f"距离上次更新PT人生数据时间不足{self._mp_push_interval}天，跳过更新")
                    return
            logger.info(f"开始更新蜂巢论坛PT人生数据...")
            cookie = self._login_and_get_cookie(proxies)
            if not cookie:
                logger.error("登录失败，无法获取cookie进行PT人生数据更新")
                return
            try:
                res = RequestUtils(cookies=cookie, proxies=proxies, timeout=30).get_res(url="https://pting.club")
            except Exception as e:
                logger.error(f"请求蜂巢出错: {str(e)}")
                return
            if not res or res.status_code != 200:
                logger.error(f"请求蜂巢返回错误状态码: {res.status_code if res else '无响应'}")
                return
            csrf_matches = re.findall(r'"csrfToken":"(.*?)"', res.text)
            if not csrf_matches:
                logger.error("获取CSRF令牌失败，无法进行PT人生数据更新")
                return
            csrf_token = csrf_matches[0]
            user_matches = re.search(r'"userId":(\d+)', res.text)
            if not user_matches:
                logger.error("获取用户ID失败，无法进行PT人生数据更新")
                return
            user_id = user_matches.group(1)
            self.__push_mp_stats(user_id=user_id, csrf_token=csrf_token, cookie=cookie)
        finally:
            self._pushing_stats = False

    def __push_mp_stats(self, user_id=None, csrf_token=None, cookie=None, retry_count=0, max_retries=3):
        """更新蜂巢论坛PT人生数据"""
        if not self._mp_push_enabled: return
        if not all([user_id, csrf_token, cookie]):
            logger.error("用户ID、CSRF令牌或Cookie为空，无法更新PT人生数据")
            return
        for attempt in range(retry_count, max_retries + 1):
            if attempt > retry_count:
                logger.info(f"更新失败，正在进行第 {attempt - retry_count}/{max_retries - retry_count} 次重试...")
                time.sleep(3)
            try:
                now = datetime.now()
                logger.info(f"开始获取站点统计数据以更新蜂巢论坛PT人生数据 (用户ID: {user_id})")
                if not hasattr(self, '_cached_stats_data') or not self._cached_stats_data or not hasattr(self, '_cached_stats_time') or (now - self._cached_stats_time).total_seconds() > 3600:
                    self._cached_stats_data = self._get_site_statistics()
                    self._cached_stats_time = now
                    logger.info("获取最新站点统计数据")
                else:
                    logger.info(f"使用缓存的站点统计数据（缓存时间：{self._cached_stats_time.strftime('%Y-%m-%d %H:%M:%S')}）")
                stats_data = self._cached_stats_data
                if not stats_data:
                    logger.error("获取站点统计数据失败，无法更新PT人生数据")
                    if attempt < max_retries: continue
                    return
                if not hasattr(self, '_cached_formatted_stats') or not self._cached_formatted_stats or not hasattr(self, '_cached_stats_time') or (now - self._cached_stats_time).total_seconds() > 3600:
                    self._cached_formatted_stats = self._format_stats_data(stats_data)
                    logger.info("格式化最新站点统计数据")
                else:
                    logger.info("使用缓存的已格式化站点统计数据")
                formatted_stats = self._cached_formatted_stats
                if not formatted_stats:
                    logger.error("格式化站点统计数据失败，无法更新PT人生数据")
                    if attempt < max_retries: continue
                    return
                sites = formatted_stats.get("sites", [])
                if len(sites) > 300:
                    logger.warning(f"站点数据过多({len(sites)}个)，将只推送做种数最多的前300个站点")
                    sites.sort(key=lambda x: x.get("seeding", 0), reverse=True)
                    formatted_stats["sites"] = sites[:300]
                headers = {"X-Csrf-Token": csrf_token, "X-Http-Method-Override": "PATCH", "Content-Type": "application/json", "Cookie": cookie}
                data = {"data": {"type": "users", "attributes": {"mpStatsSummary": json.dumps(formatted_stats.get("summary", {})), "mpStatsSites": json.dumps(formatted_stats.get("sites", []))}, "id": user_id}}
                proxies = self._get_proxies()
                url = f"https://pting.club/api/users/{user_id}"
                logger.info(f"准备更新蜂巢论坛PT人生数据: {len(formatted_stats.get('sites', []))} 个站点")
                try:
                    res = RequestUtils(headers=headers, proxies=proxies, timeout=60).post_res(url=url, json=data)
                except Exception as e:
                    logger.error(f"更新请求出错: {str(e)}")
                    if attempt < max_retries: continue
                    logger.error("所有重试都失败，放弃更新")
                    return
                if res and res.status_code == 200:
                    logger.info(f"成功更新蜂巢论坛PT人生数据: 总上传 {round(formatted_stats['summary']['total_upload'] / (1024 ** 3), 2)} GB, 总下载 {round(formatted_stats['summary']['total_download'] / (1024 ** 3), 2)} GB")
                    self._last_push_time = now.strftime('%Y-%m-%d %H:%M:%S')
                    self.save_data('last_push_time', self._last_push_time)
                    if hasattr(self, '_cached_stats_data'): self._cached_stats_data = None
                    if hasattr(self, '_cached_formatted_stats'): self._cached_formatted_stats = None
                    if hasattr(self, '_cached_stats_time'): delattr(self, '_cached_stats_time')
                    logger.info("已清除站点数据缓存，下次将获取最新数据")
                    if self._notify: self._send_notification(title="【✅ 蜂巢论坛PT人生数据更新成功】", text=f"📢 执行结果\n━━━━━━━━━━\n🕐 时间：{now.strftime('%Y-%m-%d %H:%M:%S')}\n✨ 状态：成功更新蜂巢论坛PT人生数据\n📊 站点数：{len(formatted_stats.get('sites', []))} 个\n━━━━━━━━━━")
                    return True
                else:
                    logger.error(f"更新蜂巢论坛PT人生数据失败：{res.status_code if res else '请求失败'}, 响应: {res.text[:100] if res and hasattr(res, 'text') else '无响应内容'}")
                    if attempt < max_retries: continue
                    if self._notify: self._send_notification(title="【❌ 蜂巢论坛PT人生数据更新失败】", text=f"📢 执行结果\n━━━━━━━━━━\n🕐 时间：{now.strftime('%Y-%m-%d %H:%M:%S')}\n❌ 状态：更新蜂巢论坛PT人生数据失败（已重试{attempt - retry_count}次）\n━━━━━━━━━━\n💡 可能的解决方法\n• 检查Cookie是否有效\n• 确认站点是否可访问\n• 尝试手动登录网站\n━━━━━━━━━━")
                    return False
            except Exception as e:
                logger.error(f"更新过程发生异常: {str(e)}")
                import traceback
                logger.error(f"错误详情: {traceback.format_exc()}")
                if attempt < max_retries: continue
                if self._notify: self._send_notification(title="【❌ 蜂巢论坛PT人生数据更新失败】", text=f"📢 执行结果\n━━━━━━━━━━\n🕐 时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n❌ 状态：更新蜂巢论坛PT人生数据失败（已重试{attempt - retry_count}次）\n━━━━━━━━━━\n💡 可能的解决方法\n• 检查系统网络连接\n• 确认站点是否可访问\n• 检查代码是否有错误\n━━━━━━━━━━")

    def _get_site_statistics(self):
        """获取站点统计数据"""
        try:
            from app.db.site_oper import SiteOper
            from app.helper.sites import SitesHelper
            site_oper, sites_helper = SiteOper(), SitesHelper()
            managed_sites = sites_helper.get_indexers()
            managed_site_names = [s.get("name") for s in managed_sites if s.get("name")]
            raw_data_list = site_oper.get_userdata()
            if not raw_data_list:
                logger.error("未获取到站点数据")
                return None
            data_dict = {f"{d.updated_day}_{d.name}": d for d in raw_data_list}
            data_list = sorted(list(data_dict.values()), key=lambda x: x.updated_day, reverse=True)
            site_names = set()
            latest_site_data = []
            for data in data_list:
                if data.name not in site_names and data.name in managed_site_names:
                    site_names.add(data.name)
                    latest_site_data.append(data)
            sites = []
            for site_data in latest_site_data:
                site_dict = site_data.to_dict() if hasattr(site_data, "to_dict") else site_data.__dict__
                if "_sa_instance_state" in site_dict: site_dict.pop("_sa_instance_state")
                sites.append(site_dict)
            return {"sites": sites}
        except Exception as e:
            logger.error(f"获取站点统计数据出错: {str(e)}")
            return self._get_site_statistics_via_api()

    def _get_site_statistics_via_api(self):
        """通过API获取站点统计数据（备用）"""
        try:
            from app.helper.sites import SitesHelper
            sites_helper = SitesHelper()
            managed_sites = sites_helper.get_indexers()
            managed_site_names = [s.get("name") for s in managed_sites if s.get("name")]
            api_url = f"{settings.HOST}/api/v1/site/statistics"
            headers = {"Content-Type": "application/json", "Authorization": f"Bearer {settings.API_TOKEN}"}
            res = RequestUtils(headers=headers).get_res(url=api_url)
            if res and res.status_code == 200:
                data = res.json()
                all_sites = data.get("sites", [])
                sites = [s for s in all_sites if s.get("name") in managed_site_names]
                data["sites"] = sites
                return data
            else:
                logger.error(f"获取站点统计数据失败: {res.status_code if res else '连接失败'}")
                return None
        except Exception as e:
            logger.error(f"获取站点统计数据出错: {str(e)}")
            return None

    def _format_stats_data(self, stats_data):
        """格式化站点统计数据"""
        try:
            if not stats_data or not stats_data.get("sites"): return None
            sites = stats_data.get("sites", [])
            summary = {"total_upload": 0, "total_download": 0, "total_seed": 0, "total_seed_size": 0}
            site_details = []
            for site in sites:
                if not site.get("name") or site.get("error"): continue
                upload = float(site.get("upload", 0))
                download = float(site.get("download", 0))
                summary["total_upload"] += upload
                summary["total_download"] += download
                summary["total_seed"] += int(site.get("seeding", 0))
                summary["total_seed_size"] += float(site.get("seeding_size", 0))
                site_details.append({
                    "name": site.get("name"), "username": site.get("username", ""), "user_level": site.get("user_level", ""),
                    "upload": upload, "download": download, "ratio": round(upload / download, 2) if download > 0 else float('inf'),
                    "bonus": site.get("bonus", 0), "seeding": site.get("seeding", 0), "seeding_size": site.get("seeding_size", 0)
                })
            summary["updated_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            return {"summary": summary, "sites": site_details}
        except Exception as e:
            logger.error(f"格式化站点统计数据出错: {str(e)}")
            return None

    def _login_and_get_cookie(self, proxies=None):
        """使用用户名密码登录获取cookie"""
        try:
            logger.info(f"开始使用用户名'{self._username}'登录蜂巢论坛...")
            return self._login_postman_method(proxies=proxies)
        except Exception as e:
            logger.error(f"登录过程出错: {str(e)}")
            import traceback
            logger.error(f"详细错误: {traceback.format_exc()}")
            return None

    def _login_postman_method(self, proxies=None):
        """使用Postman方式登录"""
        try:
            req = RequestUtils(proxies=proxies, timeout=30)
            proxy_info = "代理" if proxies else "直接连接"
            logger.info(f"使用Postman方式登录 (使用{proxy_info})...")
            headers = {"Accept": "*/*", "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/114.0.0.0 Safari/537.36", "Cache-Control": "no-cache"}
            try:
                res = req.get_res("https://pting.club", headers=headers)
                if not res or res.status_code != 200:
                    logger.error(f"GET请求失败，状态码: {res.status_code if res else '无响应'} (使用{proxy_info})")
                    return None
            except Exception as e:
                logger.error(f"GET请求异常 (使用{proxy_info}): {str(e)}")
                return None
            csrf_token = res.headers.get('x-csrf-token') or (re.findall(r'"csrfToken":"(.*?)"', res.text) or [None])[0]
            if not csrf_token:
                logger.error(f"无法获取CSRF令牌 (使用{proxy_info})")
                return None
            set_cookie_header = res.headers.get('set-cookie')
            if not set_cookie_header or not (session_match := re.search(r'flarum_session=([^;]+)', set_cookie_header)):
                logger.error(f"无法从set-cookie中提取session cookie (使用{proxy_info})")
                return None
            session_cookie = session_match.group(1)
            login_data = {"identification": self._username, "password": self._password, "remember": True}
            login_headers = {"Content-Type": "application/json", "X-CSRF-Token": csrf_token, "Cookie": f"flarum_session={session_cookie}", **headers}
            try:
                login_res = req.post_res(url="https://pting.club/login", json=login_data, headers=login_headers)
                if not login_res or login_res.status_code != 200:
                    logger.error(f"登录请求失败，状态码: {login_res.status_code if login_res else '无响应'} (使用{proxy_info})")
                    return None
            except Exception as e:
                logger.error(f"登录请求异常 (使用{proxy_info}): {str(e)}")
                return None
            cookie_dict = {}
            if set_cookie_header := login_res.headers.get('set-cookie'):
                if session_match := re.search(r'flarum_session=([^;]+)', set_cookie_header):
                    cookie_dict['flarum_session'] = session_match.group(1)
                if remember_match := re.search(r'flarum_remember=([^;]+)', set_cookie_header):
                    cookie_dict['flarum_remember'] = remember_match.group(1)
            if 'flarum_session' not in cookie_dict: cookie_dict['flarum_session'] = session_cookie
            cookie_str = "; ".join([f"{k}={v}" for k, v in cookie_dict.items()])
            return self._verify_cookie(req, cookie_str, proxy_info)
        except Exception as e:
            logger.error(f"Postman方式登录失败 (使用{proxy_info if proxies else '直接连接'}): {str(e)}")
            import traceback
            logger.error(f"详细错误: {traceback.format_exc()}")
            return None

    def _verify_cookie(self, req, cookie_str, proxy_info):
        """验证cookie是否有效"""
        if not cookie_str: return None
        logger.info(f"验证cookie有效性 (使用{proxy_info})...")
        headers = {"Cookie": cookie_str, "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/114.0.0.0 Safari/537.36", "Accept": "*/*", "Cache-Control": "no-cache"}
        for attempt in range(3):
            try:
                if attempt > 0:
                    logger.info(f"验证Cookie重试 {attempt}/2...")
                    time.sleep(2)
                verify_res = req.get_res("https://pting.club", headers=headers)
                if verify_res and verify_res.status_code == 200:
                    if user_matches := re.search(r'"userId":(\d+)', verify_res.text):
                        if (user_id := user_matches.group(1)) != "0":
                            logger.info(f"登录成功！获取到有效cookie，用户ID: {user_id} (使用{proxy_info})")
                            return cookie_str
                logger.warning(f"第{attempt + 1}次验证cookie失败 (使用{proxy_info})")
            except Exception as e:
                logger.warning(f"第{attempt + 1}次验证cookie请求异常 (使用{proxy_info}): {str(e)}")
        logger.error("所有 3 次cookie验证尝试均失败。")
        return None
