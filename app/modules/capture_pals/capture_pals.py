import time
from dataclasses import dataclass

import cv2

from app.common.config import config
from app.modules.automation.timer import Timer


@dataclass
class IslandProfile:
    """不同岛的默认刷新/流程参数（可被用户配置覆盖）"""
    name: str
    fixed_interval_sec: float
    patrol_refresh_interval_sec: float
    enter_wait_sec: float = 5.0  # 固定 5s（不暴露给用户）


class CapturePalsModule:
    """
    尘白抓帕鲁模块（模仿 FishingModule 风格/结构）

    支持：
      - 定点抓帕鲁：进图后不退出，按固定间隔循环抓
      - 巡逻抓帕鲁：每轮退出/重进图刷新
      - 同步抓帕鲁：伙伴岛为主，间隙插入探险岛；并在某岛到上限后自动切换策略：
            伙伴岛到上限 -> 探险岛定点抓帕鲁
            探险岛到上限 -> 伙伴岛继续抓帕鲁（不再去探险岛）
            两岛到上限 -> 结束
    """
    ENTER_WAIT_SEC = 4.0  # 进图后等待 4s
    MAX_FAILED_F_ATTEMPTS = 3  # 连续按F无效次数 -> 认为到上限
    FAILED_F_SLEEP = 1  # 每次F的间隔
    PATROL_NO_COLLECT_MAX = 3  # 巡逻模式：连续找不到F提示多少轮才停
    FIXED_NO_COLLECT_MAX = 3  # 新增：定点模式连续无F提示阈值

    def __init__(self, auto, logger):
        self.auto = auto
        self.logger = logger

        self.is_log = False
        self.stop_requested = False

        # -------------------------
        # 抓帕鲁交互（F 提示）
        # -------------------------
        self.collect_image = "app/resource/images/fishing/collect.png"
        self.collect_crop = (1506 / 1920, 684 / 1080, 1547 / 1920, 731 / 1080)
        self.collect_threshold = 0.65

        # -------------------------
        # 退出地图相关（改回图片识别）
        # -------------------------
        self.btn_exit_map_image = "app/resource/images/capture_pals/exit_map.png"
        self.btn_exit_map_crop = (1838 / 1920, 968 / 1080, 1870 / 1920,
                                  1006 / 1080)
        self.btn_exit_map_threshold = 0.5
        self.btn_exit_confirm_text = "定"
        self.btn_exit_confirm_crop = (1420 / 1920, 740 / 1080, 1505 / 1920,
                                      788 / 1080)

        # -------------------------
        # 选岛与开始作战
        # -------------------------
        self.partner_island_image = "app/resource/images/capture_pals/partner_island.png"
        self.adventure_island_image = "app/resource/images/capture_pals/adventure_island.png"

        # 伙伴岛
        self.partner_island_crop = (707 / 1920, 451 / 1080, 770 / 1920,
                                    504 / 1080)

        # 探险岛
        self.adventure_island_crop = (1318 / 1920, 558 / 1080, 1378 / 1920,
                                      612 / 1080)

        # 开始作战：text “开始”
        self.start_battle_text = "开始"
        self.start_battle_crop = (1734 / 1920, 973 / 1080, 1798 / 1920,
                                  1013 / 1080)

        # -------------------------
        # 进图成功判定：任务图标
        # -------------------------
        self.in_map_task_image = "app/resource/images/capture_pals/in_map_task.png"
        self.in_map_task_crop = (1824 / 1920, 434 / 1080, 1857 / 1920,
                                 465 / 1080)
        self.in_map_task_threshold = 0.65

        # -------------------------
        # 岛屿配置
        self.partner_profile = IslandProfile(
            name="伙伴岛",
            fixed_interval_sec=35.0,
            patrol_refresh_interval_sec=2.0,
            enter_wait_sec=self.ENTER_WAIT_SEC)
        self.adventure_profile = IslandProfile(
            name="探险岛",
            fixed_interval_sec=5 * 60.0,
            patrol_refresh_interval_sec=20 * 60.0,
            enter_wait_sec=self.ENTER_WAIT_SEC)

    def run(self):
        """
        入口状态：选岛界面（可以选择伙伴岛/探险岛的页面）
        """
        self.is_log = config.isLog.value
        state = self.wait_for_start_page(timeout_sec=120.0)
        if state == "TIMEOUT":
            self.logger.error(
                "启动失败：超时仍未检测到初始页面。请手动进入抓帕鲁选岛页面（伙伴岛/探险岛选择界面）或进入地图后再启动。")
            return

        # 如果用户已经在地图内：先退出回选岛页（只在 in_map_task 存在时才允许）
        if state == "IN_MAP":
            self.logger.info("检测到已在地图内，先退出回选岛页面以保证流程一致")
            if not self.exit_map_to_island_select():
                self.logger.error("已在地图内但退出失败，请检查退出按钮图片/确认按钮crop")
                return

        enable_partner = config.CheckBox_capture_pals_partner.value
        enable_adventure = config.CheckBox_capture_pals_adventure.value

        sync_enabled = config.CheckBox_capture_pals_sync.value

        # 覆盖岛间隔（用户可调）
        self.partner_profile.fixed_interval_sec = float(
            config.SpinBox_capture_pals_partner_fixed_interval.value)
        self.partner_profile.patrol_refresh_interval_sec = float(
            config.SpinBox_capture_pals_partner_patrol_interval.value)

        self.adventure_profile.fixed_interval_sec = float(
            config.SpinBox_capture_pals_adventure_fixed_interval.value)
        self.adventure_profile.patrol_refresh_interval_sec = float(
            config.SpinBox_capture_pals_adventure_patrol_interval.value)

        if not enable_partner and not enable_adventure:
            self.logger.error("未选择任何岛屿，无法开始抓帕鲁")
            return

        # 0=定点 1=巡逻（两岛分别设置）
        partner_mode = 0
        adventure_mode = 0
        if enable_partner:
            partner_mode = int(config.ComboBox_capture_pals_partner_mode.value)
        if enable_adventure:
            adventure_mode = int(
                config.ComboBox_capture_pals_adventure_mode.value)

        # 同步：仅当双岛都勾选且勾选同步才启用
        if sync_enabled and enable_partner and enable_adventure:
            self.logger.info("启用：同步抓帕鲁（两岛独立模式 + 到上限后自动只刷未完成岛）")
            self.sync_capture_loop(partner_mode=partner_mode,
                       adventure_mode=adventure_mode)
            return

        # 非同步：按各自模式分别运行（顺序：伙伴 -> 探险）
        if enable_partner and self.auto.running:
            if partner_mode == 0:
                self.capture_fixed_loop(self.partner_profile)
            else:
                self.capture_patrol_loop(self.partner_profile)

        if enable_adventure and self.auto.running:
            if adventure_mode == 0:
                self.capture_fixed_loop(self.adventure_profile)
            else:
                self.capture_patrol_loop(self.adventure_profile)

    def wait_for_start_page(self, timeout_sec: float = 60.0) -> str:
        """
        等待用户进入可启动页面：
        - 返回 "IN_MAP"：识别到任务图标，说明已在地图内
        - 返回 "ISLAND_SELECT"：识别到伙伴岛初始页（只用伙伴岛 crop 判定）
        - 返回 "TIMEOUT"：超时
        """
        t = Timer(timeout_sec).start()

        while True:
            self.auto.take_screenshot()

            # 1) 已在地图内
            if self.is_in_map():
                return "IN_MAP"

            # 2) 在选岛页（只判定伙伴岛区域即可）
            if self.is_on_island_select_page():
                return "ISLAND_SELECT"

            if t.reached():
                return "TIMEOUT"

            self.logger.warning("未检测到初始页面（伙伴岛选岛页/地图内任务图标）。请手动进入抓帕鲁初始页面后保持不动。")
            self.sleep_with_log(1.0)

    # =========================================================
    # 定点抓帕鲁
    # =========================================================
    def capture_fixed_loop(self, island: IslandProfile):
        self.logger.info(
            f"开始：{island.name} 定点抓帕鲁，间隔={island.fixed_interval_sec:.1f}s")

        if not self.enter_map(island):
            self.logger.error(f"{island.name}：进入地图失败，终止该岛抓帕鲁")
            return

        self.sleep_with_log(island.enter_wait_sec)

        no_collect_streak = 0

        while self.auto.running:
            result = self.capture_once(island)

            if result == "CAP_REACHED":
                self.logger.warn(f"{island.name}：检测到每日抓帕鲁上限，停止该岛定点抓帕鲁")
                break

            if result == "NO_COLLECT_HINT":
                no_collect_streak += 1
                self.logger.warning(
                    f"{island.name}：本轮未找到F抓帕鲁提示，可能在等待刷新。"
                    f"连续次数={no_collect_streak}/{self.FIXED_NO_COLLECT_MAX}"
                )
                if no_collect_streak >= self.FIXED_NO_COLLECT_MAX:
                    self.logger.error(
                        f"{island.name}：连续多个刷新周期都未出现F提示，可能站位/点位不正确。"
                        f"请调整位置后重试。停止定点模式。"
                    )
                    break
            else:
                no_collect_streak = 0

            self.sleep_with_log(island.fixed_interval_sec, f"{island.name} 等待刷新")

    # =========================================================
    # 巡逻抓帕鲁
    # =========================================================
    def capture_patrol_loop(self, island: IslandProfile):
        self.logger.info(
            f"开始：{island.name} 巡逻抓帕鲁，刷新间隔={island.patrol_refresh_interval_sec:.1f}s"
        )

        no_collect_streak = 0

        while self.auto.running:
            if not self.enter_map(island):
                self.logger.error(f"{island.name}：进入地图失败，终止该岛巡逻抓帕鲁")
                break

            self.sleep_with_log(island.enter_wait_sec)

            result = self.capture_once(island)

            if result == "CAP_REACHED":
                self.logger.warn(f"{island.name}：检测到每日抓帕鲁上限，停止该岛巡逻抓帕鲁")
                break

            if result == "NO_COLLECT_HINT":
                no_collect_streak += 1
                self.logger.warn(
                    f"{island.name}：本轮未找到F抓帕鲁提示，连续次数={no_collect_streak}/{self.PATROL_NO_COLLECT_MAX}"
                )
                if no_collect_streak >= self.PATROL_NO_COLLECT_MAX:
                    self.logger.error(
                        f"{island.name}：连续多轮找不到抓帕鲁提示，可能站位/路线不对。停止该模式。")
                    break
            else:
                no_collect_streak = 0

            if not self.exit_map_to_island_select():
                self.logger.error(f"{island.name}：退出地图失败，停止")
                break

            self.sleep_with_log(island.patrol_refresh_interval_sec,
                                f"{island.name} 巡逻刷新等待")

    def sync_capture_loop(self, partner_mode: int, adventure_mode: int):
        """
        同步抓帕鲁（改进版）：
        - 两岛各自独立模式（定点/巡逻）
        - 启动时：先去“刷新周期更长”的岛抓一轮（通常探险岛），避免先在短周期岛等待太久
        - 后续：以“刷新周期更短”的岛为常驻，并按 sync_every_sec 周期插入另一岛
        - 任一岛 CAP_REACHED 后标记 done，不再前往
        - 任一岛异常（定点NO_COLLECT直接异常；巡逻NO_COLLECT累计到阈值异常）后标记 error，不再前往
        """

        MODE_PATROL = 1

        profiles = {
            "partner": self.partner_profile,
            "adventure": self.adventure_profile,
        }
        modes = {
            "partner": int(partner_mode),
            "adventure": int(adventure_mode),
        }

        done = {"partner": False, "adventure": False}
        error = {"partner": False, "adventure": False}
        no_collect_streak = {"partner": 0, "adventure": 0}

        def island_period_sec(key: str) -> float:
            """用来比较哪个岛“刷新周期更长”"""
            prof = profiles[key]
            if modes[key] == MODE_PATROL:
                return float(prof.patrol_refresh_interval_sec)
            return float(prof.fixed_interval_sec)

        period_partner = island_period_sec("partner")
        period_adventure = island_period_sec("adventure")
        sync_every_sec = max(period_partner, period_adventure)

        self.logger.info(
            f"同步抓帕鲁：自动同步周期 sync_every_sec≈{sync_every_sec:.0f}s "
            f"(partner≈{period_partner:.0f}s, adventure≈{period_adventure:.0f}s)"
        )

        def mark_result(key: str, result: str) -> bool:
            """
            处理一次抓取结果
            返回：是否应该立刻停止当前岛（done/error/需要切岛）
            """
            prof = profiles[key]
            mode = modes[key]

            if result == "CAP_REACHED":
                done[key] = True
                self.logger.warn(f"{prof.name}：检测到每日抓帕鲁上限（同步模式将不再前往该岛）")
                return True

            if result == "NO_COLLECT_HINT":
                no_collect_streak[key] += 1
                limit = self.PATROL_NO_COLLECT_MAX if mode == MODE_PATROL else self.FIXED_NO_COLLECT_MAX

                self.logger.warning(
                    f"{prof.name}：未找到F抓帕鲁提示，连续次数={no_collect_streak[key]}/{limit}"
                    + ("（巡逻）" if mode == MODE_PATROL else "（定点，可能在等刷新）")
                )

                if no_collect_streak[key] >= limit:
                    self.logger.error(
                        f"{prof.name}：连续多轮未出现F提示，标记该岛异常并停止前往。"
                        f"{'（巡逻可能路线/站位不对）' if mode == MODE_PATROL else '（定点可能点位不对）'}"
                    )
                    error[key] = True
                    return True
            else:
                no_collect_streak[key] = 0

            return False

        def enter_island(key: str) -> bool:
            self.auto.take_screenshot()  # 防御式

            prof = profiles[key]
            if not self.is_on_island_select_page():
                if not self.exit_map_to_island_select():
                    self.logger.error("同步抓帕鲁：切换/进入前退出地图失败")
                    return False

            # 进入地图动作本身在 enter_map() 内部会 take_screenshot，但这里也可保留
            if not self.enter_map(prof):
                self.logger.error(f"同步抓帕鲁：进入{prof.name}失败，标记异常")
                error[key] = True
                return False
            self.sleep_with_log(prof.enter_wait_sec)
            return True

        def leave_to_select() -> bool:
            """
            只在“地图内”(in_map_task存在)时才允许执行退出流程；
            否则不做任何退出操作，避免在别的页面误触 ESC/退出/确定。
            """
            self.auto.take_screenshot()  # 仅用于刷新 current_screenshot

            # 1) 已经在选岛页（只要伙伴岛判定成立就算）
            if self.is_on_island_select_page():
                return True

            # 2) 只有在地图内，才允许退出回选岛页
            return self.exit_map_to_island_select()

        def do_one_round(key: str) -> str:
            """
            在某个岛执行“一轮”：
            - 定点：抓一次（不必退出，但为了切岛，需要退出到选岛）
            - 巡逻：进入->抓一次->退出（本来就要退出）
            前提：已在该岛地图内
            """
            prof = profiles[key]
            mode = modes[key]

            r = self.capture_once(prof)

            if mode == MODE_PATROL:
                # 巡逻本轮结束一定退出
                if not self.exit_map_to_island_select():
                    self.logger.error(f"{prof.name}：巡逻轮次退出失败，标记异常")
                    error[key] = True
            else:
                # 定点为了切岛/插入逻辑一致，这里也退出回选岛
                if not self.exit_map_to_island_select():
                    self.logger.error(f"{prof.name}：定点轮次退出失败，标记异常")
                    error[key] = True

            return r

        # ========= 选择起始策略：先去周期更长的岛抓一轮 =========
        long_key = "partner" if island_period_sec(
            "partner") >= island_period_sec("adventure") else "adventure"
        short_key = "adventure" if long_key == "partner" else "partner"

        self.logger.info(
            f"同步抓帕鲁：启动先处理周期更长的岛={profiles[long_key].name} "
            f"(period≈{island_period_sec(long_key):.0f}s)，常驻岛={profiles[short_key].name}"
        )

        # 先进入长周期岛抓一轮
        if not enter_island(long_key):
            # 若长周期岛进不去，尝试直接常驻短周期岛
            if not enter_island(short_key):
                self.logger.error("同步抓帕鲁：两岛均无法进入，终止")
                return
            current = short_key
        else:
            rr = do_one_round(long_key)
            stop_long = mark_result(long_key, rr)
            # 确保回到选岛界面后进入短周期岛常驻
            if not self.is_on_island_select_page():
                if not leave_to_select():
                    state = self.wait_for_start_page(timeout_sec=60.0)
                    if state != "ISLAND_SELECT":
                        self.logger.error("等待用户回到选岛页面失败，停止抓帕鲁")
                        return

            # 若短周期岛无法进入则结束
            if not enter_island(short_key):
                self.logger.error("同步抓帕鲁：常驻岛进入失败，终止")
                return

            current = short_key

        other = long_key if current == short_key else short_key
        last_switch = time.time()

        # ========= 主循环：常驻 current，按 sync_every_sec 插入 other =========
        while self.auto.running:
            # 若两岛都 done/error
            if (done["partner"] or error["partner"]) and (done["adventure"] or
                                                          error["adventure"]):
                self.logger.warn("同步抓帕鲁：两岛均已完成/异常，结束")
                return

            # 如果当前岛不可用，切到另一岛
            if done[current] or error[current]:
                current, other = other, current
                if done[current] or error[current]:
                    self.logger.warn("同步抓帕鲁：剩余可用岛不存在，结束")
                    return
                if not enter_island(current):
                    continue
                last_switch = time.time()

            prof = profiles[current]
            mode = modes[current]

            # 常驻岛执行一次抓取
            r = self.capture_once(prof)
            stop_now = mark_result(current, r)
            if stop_now:
                # 为了下一步切岛，先回选岛界面
                if not leave_to_select():
                    self.logger.error("同步抓帕鲁：状态切换前退出地图失败，终止")
                    return
                continue

            # 是否到点插入另一岛
            now = time.time()
            can_go_other = (not done[other] and not error[other])
            need_switch = can_go_other and ((now - last_switch)
                                            >= sync_every_sec)

            if mode == MODE_PATROL:
                # 巡逻：常驻岛本轮结束必须退出
                if not leave_to_select():
                    self.logger.error(f"{prof.name}：退出地图失败，停止同步")
                    return

                if need_switch:
                    # 插入 other
                    self.logger.info(
                        f"同步抓帕鲁：到达周期，插入 {profiles[other].name} 抓一轮")
                    if enter_island(other):
                        rr = do_one_round(other)
                        mark_result(other, rr)
                        last_switch = time.time()
                    # 回到 current 常驻（如果 current 仍可用）
                    if not (done[current] or error[current]):
                        if not enter_island(current):
                            continue
                else:
                    # 不插入：按本岛巡逻刷新等待后重进
                    self.sleep_with_log(prof.patrol_refresh_interval_sec,
                                        f"{prof.name} 巡逻刷新等待")
                    if not enter_island(current):
                        continue

            else:
                # 定点：默认留在图内
                if need_switch:
                    self.logger.info(
                        f"同步抓帕鲁：到达周期，插入 {profiles[other].name} 抓一轮")
                    # 定点切岛前退出
                    if not leave_to_select():
                        self.logger.error("同步抓帕鲁：定点切岛前退出地图失败，终止")
                        return
                    if enter_island(other):
                        rr = do_one_round(other)
                        mark_result(other, rr)
                        last_switch = time.time()
                    # 回 current 常驻
                    if not (done[current] or error[current]):
                        if not enter_island(current):
                            continue
                else:
                    self.sleep_with_log(prof.fixed_interval_sec,
                                        f"{prof.name} 等待刷新")

    def capture_patrol_single_round(self, island: IslandProfile):
        if not self.enter_map(island):
            self.logger.error(f"{island.name}：进入失败（单轮），跳过")
            return "ENTER_FAIL"

        self.sleep_with_log(island.enter_wait_sec)

        result = self.capture_once(island)

        if not self.exit_map_to_island_select():
            self.logger.warn(
                f"{island.name}：单轮后退出地图失败（请检查退出按钮图片/确认按钮“确定”crop）")

        return result

    # =========================================================
    # 抓帕鲁动作：C + collect提示后F，判定上限
    # =========================================================
    def capture_once(self, island: IslandProfile):
        """
        返回：
          - "OK"
          - "CAP_REACHED"（collect提示仍在 + 多次F无效）
          - "NO_COLLECT_HINT"
        """
        self.auto.press_key("c")
        self.sleep_with_log(2.0)

        if not self.wait_collect_hint(timeout_sec=3.0):
            return "NO_COLLECT_HINT"

        for _ in range(self.MAX_FAILED_F_ATTEMPTS):
            self.auto.press_key("f")
            self.sleep_with_log(self.FAILED_F_SLEEP)

            if not self.is_collect_hint_present():
                self.logger.info(f"{island.name}：成功抓到帕鲁")
                self.sleep_with_log(5.0)
                return "OK"

        self.logger.warn(
            f"{island.name}：连续按F无效({self.MAX_FAILED_F_ATTEMPTS}次)，疑似达到每日抓帕鲁上限")
        return "CAP_REACHED"

    def wait_collect_hint(self, timeout_sec: float = 3.0) -> bool:
        timeout = Timer(timeout_sec).start()
        while True:
            self.auto.take_screenshot()
            if self.is_collect_hint_present():
                return True
            if timeout.reached():
                return False
            self.sleep_with_log(0.3)

    def is_collect_hint_present(self) -> bool:
        self.auto.take_screenshot()
        return bool(
            self.auto.find_element(self.collect_image,
                                   "image",
                                   threshold=self.collect_threshold,
                                   crop=self.collect_crop,
                                   is_log=self.is_log,
                                   match_method=cv2.TM_CCOEFF_NORMED))

    def enter_map(self, island: IslandProfile) -> bool:
        """
        进入地图（更智能的容错版）：
        - 坐标点击岛按钮（不做图片匹配）
        - 点击岛后等待“开始”按钮出现；若未出现且未进图，则按 ESC 清理 UI 并重试
        - 最终仍用任务图标判定是否进图成功
        """
        timeout = Timer(25).start()

        island_img = self.partner_island_image if island.name == "伙伴岛" else self.adventure_island_image
        island_crop = self.partner_island_crop if island.name == "伙伴岛" else self.adventure_island_crop

        # 可按需调整
        START_BTN_WAIT_SEC = 3.0  # 点岛后等待“开始”出现
        RETRY_PER_LOOP = 3  # 每轮最多按 ESC 纠错重试次数
        AFTER_ESC_SLEEP = 0.4
        AFTER_CLICK_ISLAND_SLEEP = 0.5

        retry = 0

        while True:
            # 1) 先快速判定：是否已经在图内
            self.auto.take_screenshot()
            if self.is_in_map():
                self.logger.info(f"{island.name}：已进入地图（任务图标已出现）")
                return True
            # 2) 点击岛按钮（坐标）
            for _ in range(3):  # 防御式，点3次
                self.auto.take_screenshot()
                if self.auto.click_element(island_img,
                                            "image",
                                            threshold=0.5,
                                            crop=island_crop,
                                            is_log=self.is_log):
                    self.sleep_with_log(AFTER_CLICK_ISLAND_SLEEP)
                    break
                else:
                    self.logger.error(f"{island.name}：点击岛按钮失败（请检查island_text及crop）")
                    return False

            # 3) 等待“开始”按钮出现（或者期间已经进图）
            start_wait = Timer(START_BTN_WAIT_SEC).start()
            start_found = False

            while True:
                self.auto.take_screenshot()

                # 如果此时已经进图，则直接返回成功
                if self.is_in_map():
                    self.logger.info(f"{island.name}：已进入地图")
                    return True

            # 4) 若找到了“开始”，点击一次然后回到外层循环等待 in_map
                for _ in range(3):  # 防御式，点3次
                    self.auto.take_screenshot()
                    if self.auto.click_element(self.start_battle_text,
                                            "text",
                                            crop=self.start_battle_crop,
                                            is_log=self.is_log):
                        self.sleep_with_log(0.5)
                        start_found = True
                        break

                # 立刻再判一次
                if start_found:
                    self.auto.take_screenshot()
                    if self.is_in_map():
                        self.logger.info(f"{island.name}：已进入地图")
                        return True

                    # 没进图就继续 while，让它再次点岛/等开始/点开始，直到总 timeout
                else:
                    # 5) 没找到“开始”，且也不在图内, 等待
                    retry += 1
                    # 如果单轮重试次数用完，给更明确的提示（但不立刻失败，仍受总 timeout 控制）
                    if retry >= RETRY_PER_LOOP:
                        retry = 0
                        self.logger.warn(f"{island.name}：多次未出现“开始”，请检查：\n"
                                        f"1) 当前是否确实在选岛页面（伙伴岛/探险岛二选一界面）\n"
                                        f"2) start_battle_crop 是否覆盖到“开始”文字\n"
                                        f"3) 是否有弹窗/菜单遮挡（已尝试 ESC）\n"
                                        f"若仍无法自动进入，请手动点击一次“开始作战”后再启动。")

                # 6) 总超时控制
                if timeout.reached():
                    self.logger.error(
                        f"{island.name}：进入地图超时（检查：岛按钮 crop、开始按钮 crop、任务图标 crop/threshold，"
                        f"以及是否确实处于选岛界面）")
                    return False

    def is_in_map(self) -> bool:
        return bool(
            self.auto.find_element(self.in_map_task_image,
                                   "image",
                                   threshold=self.in_map_task_threshold,
                                   crop=self.in_map_task_crop,
                                   is_log=self.is_log,
                                   match_method=cv2.TM_CCOEFF_NORMED))

    def exit_map_to_island_select(self) -> bool:
        """
        退出地图回选岛界面：
          - esc
          - 点击退出（图片识别）
          - 点击确认“定”（text识别）
          - 判定回到选岛界面：检测 island.png 在两个岛 crop 中任一存在
        """
        timeout = Timer(15).start()
        while True:
            self.auto.take_screenshot()

            # 打开菜单
            if self.is_in_map():
                self.auto.press_key("esc")
                self.sleep_with_log(0.5)

                # 点击退出（图片）
                self.logger.info("尝试点击退出地图按钮")
                for _ in range(3):  # 防御式，点两次
                    self.auto.take_screenshot()
                    self.auto.click_element(self.btn_exit_map_image,
                                            "image",
                                            threshold=self.btn_exit_map_threshold,
                                            crop=self.btn_exit_map_crop,
                                            is_log=self.is_log,
                                            match_method=cv2.TM_CCOEFF_NORMED)
                    self.sleep_with_log(0.5)

                # 点击确认（text）
                self.logger.info("尝试点击确认退出按钮")
                for _ in range(3):  # 防御式，点两次
                    self.auto.take_screenshot()
                    if self.auto.click_element(self.btn_exit_confirm_text,
                                            "text",
                                            crop=self.btn_exit_confirm_crop,
                                            is_log=self.is_log):
                        self.sleep_with_log(0.5)
                        break

            # 回到选岛界面判定
            if self.is_on_island_select_page():
                self.logger.info("已回到选岛界面")
                return True

            if timeout.reached():
                self.logger.error(
                    "退出地图超时（检查：退出按钮图片/crop/threshold、确认“确定”crop、选岛判定）")
                return False

    def is_on_island_select_page(self) -> bool:
        """
        选岛页判定：只要在“伙伴岛 crop”里能识别到 island.png，就认为在正确页面
        不再匹配探险岛 crop，避免触发探险岛匹配导致的异常
        """
        self.auto.take_screenshot()

        return self.auto.find_element(self.partner_island_image,
                                      "image",
                                      threshold=0.5,
                                      crop=self.partner_island_crop,
                                      is_log=self.is_log,
                                      match_method=cv2.TM_CCOEFF_NORMED)


    def sleep_with_log(self, sec: float, msg: str = "", tick: float = 0.2):
        if sec <= 0:
            return
        if msg:
            self.logger.info(f"{msg}：{sec:.1f}s")

        end = time.monotonic() + float(sec)
        while True:
            # 触发 atoms：如果已 stop，会 raise Exception("已停止")
            self.auto.take_screenshot(is_interval=False)

            remaining = end - time.monotonic()
            if remaining <= 0:
                return

            time.sleep(min(float(tick), remaining))
