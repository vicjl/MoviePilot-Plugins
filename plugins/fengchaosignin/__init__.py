import json
import re
import time
from collections import defaultdict
from datetime import datetime, timedelta, date

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
    plugin_version = "1.3.2"
    # 插件作者
    plugin_author = "madrays & Gemini"
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
            # 【V1.3.2 优化】: 签到任务本身就会更新信息，无需重复调度。解决重复登录问题。
            self._scheduler.add_job(func=self.__signin, trigger='date',
                                    run_date=datetime.now(tz=pytz.timezone(settings.TZ)) + timedelta(seconds=3),
                                    name="蜂巢签到（单次）")
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

            res_main = RequestUtils(cookies=cookie, proxies=proxies, timeout=30).get_res(url="https://pting.club")
            if not res_main or res_main.status_code != 200:
                raise Exception(f"访问主页失败，状态码: {res_main.status_code if res_main else 'N/A'}")

            match = re.search(r'"userId":(\d+)', res_main.text)
            if not match or match.group(1) == "0":
                raise Exception("无法从主页获取有效的用户ID")

            userId = match.group(1)

            api_url = f"https://pting.club/api/users/{userId}?include=groups,badges,badges.badge.category"
            res_api = None
            try:
                # 【V1.3.2 优化】: 增加单独的异常捕获，提供更具体的失败原因
                res_api = RequestUtils(cookies=cookie, proxies=proxies, timeout=30).get_res(url=api_url)
            except Exception as req_err:
                raise Exception(f"API请求异常: {req_err}")

            if not res_api or res_api.status_code != 200:
                raise Exception(f"API请求失败，状态码: {res_api.status_code if res_api else 'N/A'}")

            user_info = res_api.json()
            self.save_data("user_info", user_info)
            self.save_data("user_info_updated_at", datetime.now().strftime('%Y-%m-%d %H:%M:%S'))
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
                raise Exception("未配置用户名密码，无法进行签到")

            # 使用循环而非递归实现重试
            for attempt in range(max_retries + 1):
                if attempt > 0:
                    logger.info(f"正在进行第 {attempt}/{max_retries} 次重试...")
                    time.sleep(3)  # 重试前等待3秒

                proxies = self._get_proxies()

                logger.info(f"开始登录蜂巢论坛获取cookie...")
                cookie = self._login_and_get_cookie(proxies)
                if not cookie:
                    if attempt < max_retries:
                        continue
                    raise Exception("登录失败，无法获取cookie")

                logger.info(f"登录成功，成功获取cookie")

                try:
                    res = RequestUtils(cookies=cookie, proxies=proxies, timeout=30).get_res(url="https://pting.club")
                except Exception as e:
                    if attempt < max_retries:
                        continue
                    raise Exception(f"连接站点出错: {e}")

                if not res or res.status_code != 200:
                    if attempt < max_retries:
                        continue
                    raise Exception(f"无法连接到站点, 状态码: {res.status_code if res else '无响应'}")

                # 【V1.3.1 修正】: 签到前检查状态，以准确判断是“签到成功”还是“已签到”
                can_checkin_before = '"canCheckin":true' in res.text
                logger.info(f"签到前状态检查: canCheckin -> {can_checkin_before}")

                csrfToken = (re.findall(r'"csrfToken":"(.*?)"', res.text) or [None])[0]
                if not csrfToken:
                    if attempt < max_retries:
                        continue
                    raise Exception("无法获取CSRF令牌")

                logger.info(f"获取csrfToken成功 {csrfToken}")

                match = re.search(r'"userId":(\d+)', res.text)
                if match and match.group(1) != "0":
                    userId = match.group(1)
                    logger.info(f"获取userid成功 {userId}")
                    if self._mp_push_enabled:
                        self.__push_mp_stats(user_id=userId, csrf_token=csrfToken, cookie=cookie)
                else:
                    if attempt < max_retries:
                        continue
                    raise Exception("无法获取用户ID")

                headers = {"X-Csrf-Token": csrfToken, "X-Http-Method-Override": "PATCH", "Cookie": cookie}
                data = {"data": {"type": "users", "attributes": {"canCheckin": False}, "id": userId}}

                try:
                    res_signin = RequestUtils(headers=headers, proxies=proxies, timeout=30).post_res(
                        url=f"https://pting.club/api/users/{userId}", json=data)
                except Exception as e:
                    if attempt < max_retries:
                        continue
                    raise Exception(f"签到请求异常: {e}")

                if not res_signin or res_signin.status_code != 200:
                    if attempt < max_retries:
                        continue
                    raise Exception(f"API请求错误, 状态码: {res_signin.status_code if res_signin else '无响应'}")

                sign_dict = res_signin.json()
                self.save_data("user_info", sign_dict)
                self.save_data("user_info_updated_at", datetime.now().strftime('%Y-%m-%d %H:%M:%S'))
                logger.info("成功获取并保存用户信息。")

                attrs = sign_dict['data']['attributes']
                money = attrs.get('money', 'N/A')
                totalContinuousCheckIn = attrs.get('totalContinuousCheckIn', 'N/A')
                lastCheckinMoney = attrs.get('lastCheckinMoney', 0)

                if can_checkin_before:
                    status_text = "签到成功"
                    reward_text = f"获得{lastCheckinMoney}花粉奖励"
                    logger.info(f"蜂巢签到成功，获得{lastCheckinMoney}花粉，当前花粉: {money}，累计签到: {totalContinuousCheckIn}")
                else:
                    status_text = "已签到"
                    reward_text = "今日已领取奖励"
                    logger.info(f"蜂巢已签到，当前花粉: {money}，累计签到: {totalContinuousCheckIn}")

                if self._notify:
                    self._send_notification(
                        title=f"【✅ 蜂巢{status_text}】",
                        text=(f"📢 执行结果\n━━━━━━━━━━\n"
                              f"🕐 时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
                              f"✨ 状态：{status_text}\n🎁 奖励：{reward_text}\n━━━━━━━━━━\n"
                              f"📊 积分统计\n🌸 花粉：{money}\n📆 签到天数：{totalContinuousCheckIn}\n━━━━━━━━━━"))

                history_record = {
                    "date": datetime.now().strftime('%Y-%m-%d %H:%M:%S'), "status": status_text,
                    "money": money, "totalContinuousCheckIn": totalContinuousCheckIn,
                    "lastCheckinMoney": lastCheckinMoney, "failure_count": 0}
                self._save_history(history_record)

                if self._current_retry > 0:
                    logger.info(f"蜂巢签到重试成功，重置重试计数")
                    self._current_retry = 0
                return True

        except Exception as e:
            logger.error(f"签到过程发生异常: {str(e)}")
            failure_history_record = {
                "date": datetime.now().strftime('%Y-%m-%d %H:%M:%S'), "status": "签到失败",
                "reason": str(e), "failure_count": 1}
            self._save_history(failure_history_record)
            self._send_signin_failure_notification(str(e), attempt)

            if self._retry_count > 0 and self._current_retry < self._retry_count:
                self._current_retry += 1
                self._schedule_retry()
            else:
                if self._retry_count > 0:
                    logger.info("已达到最大定时重试次数，不再重试")
                self._current_retry = 0
            return False
        finally:
            self._signing_in = False

    def _save_history(self, record: Dict[str, Any]):
        """
        保存签到历史记录，实现同日失败记录的更新和成功记录的覆盖。
        """
        history = self.get_data('history') or []
        today_str = date.today().strftime('%Y-%m-%d')
        
        last_today_index = -1
        for i in range(len(history) - 1, -1, -1):
            if history[i].get("date", "").startswith(today_str):
                last_today_index = i
                break
        
        is_new_success = "成功" in record.get("status", "") or "已签到" in record.get("status", "")
        
        if last_today_index != -1:
            last_record = history[last_today_index]
            is_last_success = "成功" in last_record.get("status", "") or "已签到" in last_record.get("status", "")

            if not is_new_success and not is_last_success:
                last_record["failure_count"] = last_record.get("failure_count", 0) + record.get("failure_count", 1)
                last_record["date"] = record["date"]
                last_record["reason"] = record.get("reason", "")
                logger.info(f"更新当天签到失败记录，累计失败: {last_record['failure_count']}次")
            elif is_new_success and not is_last_success:
                history[last_today_index] = record
                logger.info("签到成功，覆盖当天失败记录")
            else:
                history.append(record)
        else:
            history.append(record)

        if "失败" in record.get("status", ""):
            record["retry"] = {
                "enabled": self._retry_count > 0, "current": self._current_retry,
                "max": self._retry_count, "interval": self._retry_interval}

        if self._history_days:
            try:
                days_ago = time.time() - int(self._history_days) * 24 * 60 * 60
                history = [r for r in history if
                           datetime.strptime(r["date"], '%Y-%m-%d %H:%M:%S').timestamp() >= days_ago]
            except Exception as e:
                logger.error(f"清理历史记录异常: {str(e)}")

        self.save_data("history", history)

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
                "id": "FengchaoSignin", "name": "蜂巢签到服务",
                "trigger": CronTrigger.from_crontab(self._cron),
                "func": self.__signin, "kwargs": {}})
        if self._enabled and self._mp_push_enabled:
            services.append({
                "id": "MoviePilotStatsPush", "name": "蜂巢论坛PT人生数据更新服务", "trigger": "interval",
                "func": self.__check_and_push_mp_stats, "kwargs": {"hours": 6}})
        return services

    def get_form(self) -> Tuple[List[dict], Dict[str, Any]]:
        return [
            {'component': 'VForm', 'content': [
                {'component': 'VCard', 'props': {'variant': 'outlined', 'class': 'mt-3'}, 'content': [
                    {'component': 'VCardTitle', 'props': {'class': 'd-flex align-center'}, 'content': [
                        {'component': 'VIcon', 'props': {'style': 'color: #1976D2;', 'class': 'mr-2'}, 'text': 'mdi-calendar-check'},
                        {'component': 'span', 'text': '蜂巢签到设置'}]},
                    {'component': 'VDivider'},
                    {'component': 'VCardText', 'content': [
                        {'component': 'VRow', 'content': [
                            {'component': 'VCol', 'props': {'cols': 12, 'md': 4}, 'content': [{'component': 'VSwitch', 'props': {'model': 'enabled', 'label': '启用插件'}}]},
                            {'component': 'VCol', 'props': {'cols': 12, 'md': 4}, 'content': [{'component': 'VSwitch', 'props': {'model': 'notify', 'label': '开启通知'}}]},
                        ]},
                        {'component': 'VRow', 'content': [
                            {'component': 'VCol', 'props': {'cols': 12, 'md': 4}, 'content': [{'component': 'VSwitch', 'props': {'model': 'onlyonce', 'label': '立即运行一次', 'hint': '同时执行签到和信息更新任务'}}]},
                            {'component': 'VCol', 'props': {'cols': 12, 'md': 4}, 'content': [{'component': 'VSwitch', 'props': {'model': 'update_info_now', 'label': '立即更新个人信息', 'hint': '不执行签到，仅刷新插件页面显示的用户信息'}}]}
                        ]},
                        {'component': 'VRow', 'content': [
                            {'component': 'VCol', 'props': {'cols': 12, 'md': 6}, 'content': [{'component': 'VTextField', 'props': {'model': 'username', 'label': '用户名', 'placeholder': '蜂巢论坛用户名', 'hint': 'Cookie失效时自动登录获取新Cookie'}}]},
                            {'component': 'VCol', 'props': {'cols': 12, 'md': 6}, 'content': [{'component': 'VTextField', 'props': {'model': 'password', 'label': '密码', 'placeholder': '蜂巢论坛密码', 'type': 'password', 'hint': 'Cookie失效时自动登录获取新Cookie'}}]}
                        ]},
                        {'component': 'VRow', 'content': [
                            {'component': 'VCol', 'props': {'cols': 12, 'md': 6}, 'content': [{'component': 'VCronField', 'props': {'model': 'cron', 'label': '签到周期', 'placeholder': '30 8 * * *', 'hint': '五位cron表达式，每天早上8:30执行'}}]},
                            {'component': 'VCol', 'props': {'cols': 12, 'md': 6}, 'content': [{'component': 'VTextField', 'props': {'model': 'history_days', 'label': '历史保留天数', 'placeholder': '30', 'hint': '历史记录保留天数'}}]}
                        ]},
                        {'component': 'VRow', 'content': [
                            {'component': 'VCol', 'props': {'cols': 12, 'md': 6}, 'content': [{'component': 'VTextField', 'props': {'model': 'retry_count', 'label': '失败重试次数', 'type': 'number', 'placeholder': '0', 'hint': '0表示不重试，大于0则在签到失败后重试'}}]},
                            {'component': 'VCol', 'props': {'cols': 12, 'md': 6}, 'content': [{'component': 'VTextField', 'props': {'model': 'retry_interval', 'label': '重试间隔(小时)', 'type': 'number', 'placeholder': '2', 'hint': '签到失败后多少小时后重试'}}]}
                        ]},
                        {'component': 'VRow', 'content': [
                            {'component': 'VCol', 'props': {'cols': 12, 'md': 6}, 'content': [{'component': 'VSwitch', 'props': {'model': 'use_proxy', 'label': '使用代理', 'hint': '与蜂巢论坛通信时使用系统代理'}}]}
                        ]},
                        {'component': 'VDivider', 'props': {'class': 'my-3'}},
                        {'component': 'VRow', 'content': [
                            {'component': 'VCol', 'props': {'cols': 12}, 'content': [
                                {'component': 'div', 'props': {'class': 'd-flex align-center mb-3'}, 'content': [
                                    {'component': 'VIcon', 'props': {'style': 'color: #1976D2;', 'class': 'mr-2'}, 'text': 'mdi-chart-box'},
                                    {'component': 'span', 'props': {'style': 'font-size: 1.1rem; font-weight: 500;'}, 'text': '蜂巢个人主页PT人生卡片数据更新'}]}]}]},
                        {'component': 'VRow', 'content': [
                            {'component': 'VCol', 'props': {'cols': 12}, 'content': [{'component': 'VSwitch', 'props': {'model': 'mp_push_enabled', 'label': '启用PT人生数据更新', 'hint': '每次签到时都会自动更新PT人生数据'}}]}
                        ]}]}]}
            ]}
        ], {"enabled": False, "notify": True, "cron": "30 8 * * *", "onlyonce": False, "update_info_now": False, "cookie": "", "username": "", "password": "", "history_days": 30, "retry_count": 0, "retry_interval": 2, "mp_push_enabled": False, "mp_push_interval": 1, "use_proxy": True}

    def _map_fa_to_mdi(self, icon_class: str) -> str:
        if not icon_class or not isinstance(icon_class, str): return 'mdi-account-group'
        if icon_class.startswith('mdi-'): return icon_class
        mapping = {
            'fa-user-tie': 'mdi-account-tie', 'fa-crown': 'mdi-crown', 'fa-shield-alt': 'mdi-shield-outline',
            'fa-user-shield': 'mdi-account-shield', 'fa-user-cog': 'mdi-account-cog',
            'fa-user-check': 'mdi-account-check', 'fa-fan': 'mdi-fan', 'fa-user': 'mdi-account',
            'fa-users': 'mdi-account-group', 'fa-cogs': 'mdi-cog', 'fa-cog': 'mdi-cog', 'fa-star': 'mdi-star',
            'fa-gem': 'mdi-diamond'}
        match = re.search(r'fa-[\w-]+', icon_class)
        if match: return mapping.get(match.group(0), 'mdi-account-group')
        return 'mdi-account-group'

    def _format_pollen(self, value: Any) -> str:
        if value is None: return '—'
        try:
            num = float(value)
            return str(int(num)) if num == int(num) else f'{round(num, 3):g}'
        except (ValueError, TypeError): return str(value)

    def get_page(self) -> List[dict]:
        history = self.get_data('history') or []
        user_info = self.get_data('user_info')
        user_info_updated_at = self.get_data('user_info_updated_at')
        pt_life_updated_at = self.get_data('last_push_time')
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
            unread_notifications = user_attrs.get('unreadNotificationCount', 0)

            try:
                join_time = datetime.fromisoformat(join_time_str.replace('Z', '+00:00')).strftime('%Y-%m-%d')
            except: join_time = '未知'
            try:
                last_seen_at = datetime.fromisoformat(last_seen_at_str.replace('Z', '+00:00')).strftime('%Y-%m-%d %H:%M')
            except: last_seen_at = '未知'

            groups = [{'name': item['attributes'].get('nameSingular', ''), 'color': item['attributes'].get('color', '#888'), 'icon': self._map_fa_to_mdi(item['attributes'].get('icon', ''))} for item in user_info.get('included', []) if item.get('type') == 'groups']
            
            badges = []
            for badge_item in user_attrs.get('badges', []):
                core_badge_info = badge_item.get('badge', {})
                if not core_badge_info: continue
                badges.append({'name': core_badge_info.get('name', '未知徽章'), 'icon': core_badge_info.get('icon', 'fas fa-award'), 'description': core_badge_info.get('description', '无描述'), 'image': core_badge_info.get('image'), 'category': core_badge_info.get('category', {}).get('name', '其他')})
            badge_count = len(badges)
            
            categorized_badges = defaultdict(list)
            for badge in badges: categorized_badges[badge['category']].append(badge)

            badge_category_components = []
            if categorized_badges:
                all_category_cards = []
                for category_name, badge_list in sorted(categorized_badges.items()):
                    badge_items = []
                    for badge in badge_list:
                        badge_items.append({'component': 'div', 'props': {'class': 'ma-1 pa-1 d-flex flex-column align-center', 'style': 'width: 90px; text-align: center;', 'title': f"{badge.get('name', '未知徽章')}\n\n{badge.get('description', '无描述')}"}, 'content': [{'component': 'VImg' if badge.get('image') else 'VIcon', 'props': ({'src': badge.get('image'), 'height': '48', 'width': '48', 'class': 'mb-1'} if badge.get('image') else {'icon': self._map_fa_to_mdi(badge.get('icon')), 'size': '48', 'class': 'mb-1'})}, {'component': 'div', 'props': {'class': 'text-caption text-truncate', 'style': 'max-width: 90px; line-height: 20px; font-weight: 500;'}, 'text': badge.get('name', '未知徽章')}]})
                    
                    badge_items_with_dividers = []
                    for i, item in enumerate(badge_items):
                        badge_items_with_dividers.append(item)
                        if i < len(badge_items) - 1:
                            badge_items_with_dividers.append({'component': 'VDivider', 'props': {'vertical': True, 'class': 'my-2'}})

                    all_category_cards.append({'component': 'div', 'props': {'class': 'ma-1 pa-2', 'style': f'{frost_style} border-radius: 12px;'}, 'content': [{'component': 'div', 'props': {'class': 'text-subtitle-2 grey--text text--darken-1', 'style': 'text-align: center;'}, 'text': category_name}, {'component': 'VDivider', 'props': {'class': 'my-1'}}, {'component': 'div', 'props': {'class': 'd-flex flex-wrap justify-center align-center'}, 'content': badge_items_with_dividers}]})
                badge_category_components.append({'component': 'div', 'props': {'class': 'd-flex flex-wrap'}, 'content': all_category_cards})
            
            username_display_content = [{'component': 'div', 'props': {'class': 'text-h6 mb-1 pa-2 d-inline-block elevation-2', 'style': frost_style}, 'text': username}]
            if unread_notifications > 0:
                username_display_content.append({'component': 'VBadge', 'props': {'color': 'red', 'content': str(unread_notifications), 'inline': True, 'class': 'ml-2'}, 'content': [{'component': 'VIcon', 'props': {'color': 'white'}, 'text': 'mdi-bell'}]})
            
            footer_texts = [f'最后签到: {last_checkin_time}']
            if user_info_updated_at: footer_texts.append(f'数据更新: {user_info_updated_at}')
            if pt_life_updated_at: footer_texts.append(f'PT人生更新: {pt_life_updated_at}')
            footer_line = ' • '.join(footer_texts)

            stats_grid = []
            stats_items = [
                ('花粉', str(money), 'mdi-flower', '#FFC107'), ('主题', str(discussion_count), 'mdi-forum', '#3F51B5'),
                ('评论', str(comment_count), 'mdi-comment-text-multiple', '#00BCD4'), ('粉丝', str(follower_count), 'mdi-account-group', '#673AB7'),
                ('关注', str(following_count), 'mdi-account-multiple-plus', '#03A9F4'), ('连续签到', str(total_continuous_checkin), 'mdi-calendar-check', '#009688')]
            for title, value, icon, color in stats_items:
                stats_grid.append({'component': 'VCol', 'props': {'cols': 6, 'sm': 4}, 'content': [{'component': 'div', 'props': {'class': 'text-center pa-2 elevation-2', 'style': frost_style}, 'content': [{'component': 'div', 'props': {'class': 'd-flex justify-center align-center'}, 'content': [{'component': 'VIcon', 'props': {'style': f'color: {color};', 'class': 'mr-1'}, 'text': icon}, {'component': 'span', 'props': {'class': 'text-h6'}, 'text': value}]}, {'component': 'div', 'props': {'class': 'text-caption mt-1'}, 'text': title}]}]})
            
            user_info_card = {'component': 'VCard', 'props': {'variant': 'outlined', 'class': 'mb-4', 'style': f"background-image: url('{background_image}'); background-size: cover; background-position: center;" if background_image else ''}, 'content': [
                {'component': 'VDivider'}, {'component': 'VCardText', 'content': [
                    {'component': 'VRow', 'props': {'class': 'ma-1'}, 'content': [
                        {'component': 'VCol', 'props': {'cols': 12, 'md': 5}, 'content': [
                            {'component': 'div', 'props': {'class': 'd-flex align-center'}, 'content': [
                                {'component': 'div', 'props': {'class': 'mr-3', 'style': 'position: relative; width: 90px; height: 90px;'}, 'content': [{'component': 'VAvatar', 'props': {'size': 60, 'rounded': 'circle', 'style': 'position: absolute; top: 15px; left: 15px; z-index: 1;'}, 'content': [{'component': 'VImg', 'props': {'src': avatar_url, 'alt': username}}]}, {'component': 'div', 'props': {'style': f"position: absolute; top: 0; left: 0; width: 90px; height: 90px; background-image: url('{user_attrs.get('decorationAvatarFrame', '')}'); background-size: contain; background-repeat: no-repeat; background-position: center; z-index: 2;"}} if user_attrs.get('decorationAvatarFrame') else {}]},
                                {'component': 'div', 'content': [{'component': 'div', 'props': {'class': 'd-flex align-center'}, 'content': username_display_content}, {'component': 'div', 'props': {'class': 'd-flex flex-wrap mt-1'}, 'content': [{'component': 'VChip', 'props': {'style': f"background-color: {group['color']}; color: white;", 'size': 'small', 'class': 'mr-1 mb-1', 'variant': 'elevated'}, 'content': [{'component': 'VIcon', 'props': {'start': True, 'size': 'small'}, 'text': group['icon']}, {'component': 'span', 'text': group['name']}]} for group in groups]}]}]},
                            {'component': 'VRow', 'props': {'class': 'mt-2'}, 'content': [
                                {'component': 'VCol', 'props': {'cols': 12}, 'content': [
                                    {'component': 'div', 'props': {'class': 'pa-1 elevation-2 mb-1', 'style': f'{frost_style} width: fit-content;'}, 'content': [{'component': 'div', 'props': {'class': 'd-flex align-center text-caption'}, 'content': [{'component': 'VIcon', 'props': {'style': 'color: #4CAF50;', 'size': 'x-small', 'class': 'mr-1'}, 'text': 'mdi-calendar'}, {'component': 'span', 'text': f'注册于 {join_time}'}]}]},
                                    {'component': 'div', 'props': {'class': 'pa-1 elevation-2 mb-1', 'style': f'{frost_style} width: fit-content;'}, 'content': [{'component': 'div', 'props': {'class': 'd-flex align-center text-caption'}, 'content': [{'component': 'VIcon', 'props': {'style': 'color: #2196F3;', 'size': 'x-small', 'class': 'mr-1'}, 'text': 'mdi-clock-outline'}, {'component': 'span', 'text': f'最后访问 {last_seen_at}'}]}]},
                                    {'component': 'div', 'props': {'class': 'pa-1 elevation-2', 'style': f'{frost_style} width: fit-content;'}, 'content': [{'component': 'div', 'props': {'class': 'd-flex align-center text-caption'}, 'content': [{'component': 'VIcon', 'props': {'style': 'color: #E64A19;', 'size': 'x-small', 'class': 'mr-1'}, 'text': 'mdi-medal-outline'}, {'component': 'span', 'text': f'拥有 {badge_count} 枚徽章'}]}]}]}]},
                        {'component': 'VCol', 'props': {'cols': 12, 'md': 7}, 'content': [{'component': 'VRow', 'content': stats_grid}]}]},
                    *badge_category_components,
                    {'component': 'div', 'props': {'class': 'mt-2 text-caption text-right pa-1 elevation-2 d-inline-block float-right', 'style': frost_style}, 'text': footer_line}]}]}

        if not history:
            components = []
            if user_info_card: components.append(user_info_card)
            components.extend([
                {'component': 'VAlert', 'props': {'type': 'info', 'variant': 'tonal', 'text': '暂无签到记录，请先配置用户名和密码并启用插件', 'class': 'mb-2', 'prepend-icon': 'mdi-information'}},
                {'component': 'VCard', 'props': {'variant': 'outlined', 'class': 'mb-4'}, 'content': [
                    {'component': 'VCardTitle', 'props': {'class': 'd-flex align-center'}, 'content': [{'component': 'VIcon', 'props': {'color': 'amber-darken-2', 'class': 'mr-2'}, 'text': 'mdi-flower'}, {'component': 'span', 'props': {'class': 'text-h6'}, 'text': '签到奖励说明'}]},
                    {'component': 'VDivider'},
                    {'component': 'VCardText', 'props': {'class': 'pa-3'}, 'content': [
                        {'component': 'div', 'props': {'class': 'd-flex align-center mb-2'}, 'content': [{'component': 'VIcon', 'props': {'style': 'color: #FF8F00;', 'size': 'small', 'class': 'mr-2'}, 'text': 'mdi-check-circle'}, {'component': 'span', 'text': '每日签到可获得随机花粉奖励'}]},
                        {'component': 'div', 'props': {'class': 'd-flex align-center'}, 'content': [{'component': 'VIcon', 'props': {'style': 'color: #1976D2;', 'size': 'small', 'class': 'mr-2'}, 'text': 'mdi-calendar-check'}, {'component': 'span', 'text': '连续签到可累积天数，提升论坛等级'}]}]}]}])
            return components

        history = sorted(history, key=lambda x: x.get("date", ""), reverse=True)
        history_rows = []
        status_colors = {"签到成功": "#4CAF50", "已签到": "#2196F3", "签到失败": "#F44336"}
        status_icons = {"签到成功": "mdi-check-circle", "已签到": "mdi-information-outline", "签到失败": "mdi-close-circle"}
        
        for record in history:
            status_text = record.get("status", "未知")
            failure_count_text = str(record.get('failure_count', '—')) if status_text == "签到失败" else "—"
            history_rows.append({'component': 'tr', 'content': [
                    {'component': 'td', 'props': {'class': 'text-caption'}, 'text': record.get("date", "")},
                    {'component': 'td', 'content': [{'component': 'VChip', 'props': {'color': status_colors.get(status_text, "grey"), 'size': 'small', 'variant': 'elevated'}, 'content': [{'component': 'VIcon', 'props': {'start': True, 'size': 'small'}, 'text': status_icons.get(status_text, "mdi-help-circle")}, {'component': 'span', 'text': status_text}]}, {'component': 'div', 'props': {'class': 'mt-1 text-caption grey--text'}, 'text': f"将在{record.get('retry', {}).get('interval', self._retry_interval)}小时后重试 ({record.get('retry', {}).get('current', 0)}/{record.get('retry', {}).get('max', self._retry_count)})" if status_text == "签到失败" and record.get('retry', {}).get('enabled', False) and record.get('retry', {}).get('current', 0) > 0 else ""}]},
                    {'component': 'td', 'text': failure_count_text},
                    {'component': 'td', 'content': [{'component': 'div', 'props': {'class': 'd-flex align-center'}, 'content': [{'component': 'VIcon', 'props': {'style': 'color: #FFC107;', 'class': 'mr-1'}, 'text': 'mdi-flower'}, {'component': 'span', 'text': self._format_pollen(record.get('money'))}]}]},
                    {'component': 'td', 'content': [{'component': 'div', 'props': {'class': 'd-flex align-center'}, 'content': [{'component': 'VIcon', 'props': {'style': 'color: #1976D2;', 'class': 'mr-1'}, 'text': 'mdi-calendar-check'}, {'component': 'span', 'text': record.get('totalContinuousCheckIn', '—')}]}]},
                    {'component': 'td', 'content': [{'component': 'div', 'props': {'class': 'd-flex align-center'}, 'content': [{'component': 'VIcon', 'props': {'style': 'color: #FF8F00;', 'class': 'mr-1'}, 'text': 'mdi-gift'}, {'component': 'span', 'text': f"{self._format_pollen(record.get('lastCheckinMoney', 0))}花粉" if "成功" in status_text and record.get('lastCheckinMoney', 0) > 0 else '—'}]}]}]})

        components = []
        if user_info_card: components.append(user_info_card)
        components.append({'component': 'VCard', 'props': {'variant': 'outlined', 'class': 'mb-4'}, 'content': [
                {'component': 'VCardTitle', 'props': {'class': 'd-flex align-center'}, 'content': [{'component': 'VIcon', 'props': {'style': 'color: #9C27B0;', 'class': 'mr-2'}, 'text': 'mdi-history'}, {'component': 'span', 'props': {'class': 'text-h6 font-weight-bold'}, 'text': '蜂巢签到历史'}, {'component': 'VSpacer'}, {'component': 'VChip', 'props': {'color': 'orange', 'size': 'small', 'variant': 'elevated'}, 'content': [{'component': 'VIcon', 'props': {'start': True, 'size': 'small'}, 'text': 'mdi-flower'}, {'component': 'span', 'text': '每日可得花粉奖励'}]}]},
                {'component': 'VDivider'},
                {'component': 'VCardText', 'props': {'class': 'pa-0 pa-md-2'}, 'content': [{'component': 'VResponsive', 'content': [{'component': 'VTable', 'props': {'hover': True, 'density': 'comfortable'}, 'content': [{'component': 'thead', 'content': [{'component': 'tr', 'content': [{'component': 'th', 'text': '时间'}, {'component': 'th', 'text': '状态'}, {'component': 'th', 'text': '失败次数'}, {'component': 'th', 'text': '花粉'}, {'component': 'th', 'text': '签到天数'}, {'component': 'th', 'text': '奖励'}]}]}, {'component': 'tbody', 'content': history_rows}]}]}]}]})
        components.append({'component': 'style', 'text': """.v-table { border-radius: 8px; overflow: hidden; } .v-table th { background-color: rgba(var(--v-theme-primary), 0.05); color: rgb(var(--v-theme-primary)); font-weight: 600; }"""})
        return components

    def stop_service(self):
        try:
            if self._scheduler:
                self._scheduler.remove_all_jobs()
                if self._scheduler.running:
                    self._scheduler.shutdown()
                self._scheduler = None
        except Exception as e:
            logger.error(f"退出插件失败：{e}")

    def __check_and_push_mp_stats(self):
        if hasattr(self, '_pushing_stats') and self._pushing_stats:
            logger.info("已有更新PT人生数据任务在执行，跳过当前任务")
            return
        self._pushing_stats = True
        try:
            if not self._mp_push_enabled: return
            if not self._username or not self._password:
                logger.error("未配置用户名密码，无法更新PT人生数据")
                return
            
            now = datetime.now()
            if self._last_push_time:
                last_push = datetime.strptime(self._last_push_time, '%Y-%m-%d %H:%M:%S')
                if (now - last_push).days < self._mp_push_interval:
                    logger.info(f"距离上次更新PT人生数据时间不足{self._mp_push_interval}天，跳过更新")
                    return
            
            logger.info(f"开始更新蜂巢论坛PT人生数据...")
            proxies = self._get_proxies()
            cookie = self._login_and_get_cookie(proxies)
            if not cookie:
                logger.error("登录失败，无法获取cookie进行PT人生数据更新")
                return

            res = RequestUtils(cookies=cookie, proxies=proxies, timeout=30).get_res(url="https://pting.club")
            if not res or res.status_code != 200:
                logger.error(f"请求蜂巢返回错误状态码: {res.status_code if res else '无响应'}")
                return

            csrf_token = (re.findall(r'"csrfToken":"(.*?)"', res.text) or [None])[0]
            user_id = (re.search(r'"userId":(\d+)', res.text).group(1) or None)
            if not csrf_token or not user_id:
                logger.error("获取CSRF令牌或用户ID失败，无法进行PT人生数据更新")
                return
            
            self.__push_mp_stats(user_id=user_id, csrf_token=csrf_token, cookie=cookie)
        finally:
            self._pushing_stats = False

    def __push_mp_stats(self, user_id=None, csrf_token=None, cookie=None):
        if not self._mp_push_enabled or not all([user_id, csrf_token, cookie]):
            return

        for attempt in range(3):
            try:
                now = datetime.now()
                logger.info(f"开始获取站点统计数据以更新蜂巢论坛PT人生数据 (用户ID: {user_id})")
                
                stats_data = self._get_site_statistics()
                if not stats_data:
                    raise Exception("获取站点统计数据失败")

                formatted_stats = self._format_stats_data(stats_data)
                if not formatted_stats:
                    raise Exception("格式化站点统计数据失败")

                sites = formatted_stats.get("sites", [])
                if len(sites) > 300:
                    logger.warning(f"站点数据过多({len(sites)}个)，将只推送做种数最多的前300个站点")
                    sites.sort(key=lambda x: x.get("seeding", 0), reverse=True)
                    formatted_stats["sites"] = sites[:300]
                
                headers = {"X-Csrf-Token": csrf_token, "X-Http-Method-Override": "PATCH", "Content-Type": "application/json", "Cookie": cookie}
                data = {"data": {"type": "users", "attributes": {"mpStatsSummary": json.dumps(formatted_stats.get("summary", {})), "mpStatsSites": json.dumps(formatted_stats.get("sites", []))}, "id": user_id}}
                proxies = self._get_proxies()
                url = f"https://pting.club/api/users/{user_id}"
                
                logger.info(f"准备更新蜂巢论坛PT人生数据: {len(sites)} 个站点")
                res = RequestUtils(headers=headers, proxies=proxies, timeout=60).post_res(url=url, json=data)

                if res and res.status_code == 200:
                    summary = formatted_stats['summary']
                    logger.info(f"成功更新蜂巢论坛PT人生数据: 总上传 {round(summary['total_upload'] / (1024 ** 4), 2)} TB, 总下载 {round(summary['total_download'] / (1024 ** 4), 2)} TB")
                    self._last_push_time = now.strftime('%Y-%m-%d %H:%M:%S')
                    self.save_data('last_push_time', self._last_push_time)
                    if self._notify: self._send_notification(title="【✅ 蜂巢论坛PT人生数据更新成功】", text=f"📢 执行结果\n━━━━━━━━━━\n🕐 时间：{self._last_push_time}\n✨ 状态：成功更新\n📊 站点数：{len(sites)} 个\n━━━━━━━━━━")
                    return
                else:
                    raise Exception(f"更新请求失败: {res.status_code if res else 'N/A'}, 响应: {res.text[:100] if res and hasattr(res, 'text') else '无'}")

            except Exception as e:
                logger.error(f"更新PT人生数据第 {attempt + 1}/3 次尝试失败: {e}")
                if attempt < 2:
                    time.sleep(5)
                else:
                    if self._notify: self._send_notification(title="【❌ 蜂巢论坛PT人生数据更新失败】", text=f"📢 执行结果\n━━━━━━━━━━\n🕐 时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n❌ 状态：多次尝试后更新失败\n💬 原因：{e}\n━━━━━━━━━━")

    def _get_site_statistics(self):
        try:
            from app.db.site_oper import SiteOper
            raw_data_list = SiteOper().get_userdata()
            if not raw_data_list:
                logger.error("通过SiteOper未获取到站点数据")
                return None
            
            data_dict = {f"{d.updated_day}_{d.name}": d for d in raw_data_list}
            latest_site_data = []
            for data in sorted(data_dict.values(), key=lambda x: x.updated_day, reverse=True):
                if data.name not in [s['name'] for s in latest_site_data]:
                    latest_site_data.append(data.to_dict())
            
            from app.helper.sites import SitesHelper
            managed_site_names = {s["name"] for s in SitesHelper().get_indexers() if s.get("name")}
            sites = [s for s in latest_site_data if s.get("name") in managed_site_names]

            return {"sites": sites}
        except Exception as e:
            logger.error(f"获取站点统计数据出错: {e}，尝试API备用方案...")
            return self._get_site_statistics_via_api()

    def _get_site_statistics_via_api(self):
        try:
            api_url = f"{settings.HOST}/api/v1/site/statistics"
            headers = {"Content-Type": "application/json", "Authorization": f"Bearer {settings.API_TOKEN}"}
            res = RequestUtils(headers=headers).get_res(url=api_url)
            if res and res.status_code == 200:
                data = res.json()
                from app.helper.sites import SitesHelper
                managed_site_names = {s["name"] for s in SitesHelper().get_indexers() if s.get("name")}
                data["sites"] = [s for s in data.get("sites", []) if s.get("name") in managed_site_names]
                return data
            else:
                logger.error(f"通过API获取站点统计数据失败: {res.status_code if res else '连接失败'}")
                return None
        except Exception as e:
            logger.error(f"通过API获取站点统计数据出错: {e}")
            return None

    def _format_stats_data(self, stats_data):
        try:
            if not stats_data or not stats_data.get("sites"): return None
            sites = [s for s in stats_data.get("sites", []) if s.get("name") and not s.get("error")]
            summary = {"total_upload": sum(float(s.get("upload", 0)) for s in sites),
                       "total_download": sum(float(s.get("download", 0)) for s in sites),
                       "total_seed": sum(int(s.get("seeding", 0)) for s in sites),
                       "total_seed_size": sum(float(s.get("seeding_size", 0)) for s in sites),
                       "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S")}
            site_details = []
            for site in sites:
                upload = float(site.get("upload", 0))
                download = float(site.get("download", 0))
                site_details.append({
                    "name": site.get("name"), "username": site.get("username", ""),
                    "user_level": site.get("user_level", ""), "upload": upload, "download": download,
                    "ratio": round(upload / download, 2) if download > 0 else 0,
                    "bonus": site.get("bonus", 0), "seeding": site.get("seeding", 0),
                    "seeding_size": site.get("seeding_size", 0)})
            return {"summary": summary, "sites": site_details}
        except Exception as e:
            logger.error(f"格式化站点统计数据出错: {e}")
            return None

    def _login_and_get_cookie(self, proxies=None):
        try:
            logger.info(f"开始使用用户名'{self._username}'登录蜂巢论坛...")
            return self._login_postman_method(proxies=proxies)
        except Exception as e:
            logger.error(f"登录过程出错: {e}")
            return None

    def _login_postman_method(self, proxies=None):
        try:
            req = RequestUtils(proxies=proxies, timeout=30)
            proxy_info = "代理" if proxies else "直接连接"
            logger.info(f"使用Postman方式登录 (使用{proxy_info})...")
            
            res = req.get_res("https://pting.club")
            if not res or res.status_code != 200:
                logger.error(f"获取初始页面失败，状态码: {res.status_code if res else '无响应'} ({proxy_info})")
                return None

            csrf_token = (re.findall(r'"csrfToken":"(.*?)"', res.text) or [None])[0]
            session_cookie = (re.search(r'flarum_session=([^;]+)', res.headers.get('set-cookie', '')) or [None])[0]
            
            if not csrf_token or not session_cookie:
                logger.error(f"无法获取CSRF令牌或Session Cookie ({proxy_info})")
                return None

            login_data = {"identification": self._username, "password": self._password, "remember": True}
            login_headers = {"Content-Type": "application/json", "X-CSRF-Token": csrf_token, "Cookie": f"flarum_session={session_cookie}"}
            
            login_res = req.post_res(url="https://pting.club/login", json=login_data, headers=login_headers)
            if not login_res or login_res.status_code != 200:
                logger.error(f"登录请求失败，状态码: {login_res.status_code if login_res else '无响应'} ({proxy_info})")
                return None

            cookie_dict = {'flarum_session': (re.search(r'flarum_session=([^;]+)', login_res.headers.get('set-cookie', '')) or [session_cookie])[0]}
            if remember_match := re.search(r'flarum_remember=([^;]+)', login_res.headers.get('set-cookie', '')):
                cookie_dict['flarum_remember'] = remember_match.group(1)

            cookie_str = "; ".join([f"{k}={v}" for k, v in cookie_dict.items()])
            return self._verify_cookie(req, cookie_str, proxy_info)
        except Exception as e:
            logger.error(f"Postman方式登录失败: {e}")
            return None

    def _verify_cookie(self, req, cookie_str, proxy_info):
        if not cookie_str: return None
        logger.info(f"验证cookie有效性 (使用{proxy_info})...")
        for attempt in range(2):
            try:
                verify_res = req.get_res("https://pting.club", headers={"Cookie": cookie_str})
                if verify_res and verify_res.status_code == 200:
                    if (user_id_match := re.search(r'"userId":(\d+)', verify_res.text)) and user_id_match.group(1) != "0":
                        logger.info(f"登录成功！获取到有效cookie，用户ID: {user_id_match.group(1)} ({proxy_info})")
                        return cookie_str
                logger.warning(f"第 {attempt + 1}/2 次验证cookie失败 ({proxy_info})")
            except Exception as e:
                logger.warning(f"第 {attempt + 1}/2 次验证cookie请求异常: {e}")
            if attempt < 1: time.sleep(2)
        logger.error("所有cookie验证尝试均失败。")
        return None
