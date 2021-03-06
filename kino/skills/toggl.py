
import arrow
from hbconfig import Config

from ..open_api.toggl import Toggl

from ..slack.resource import MsgResource
from ..slack.slackbot import SlackerAdapter

from ..skills.question import AttentionQuestion
from ..skills.todoist import TodoistManager

from ..utils.arrow import ArrowUtil
from ..utils.data_handler import DataHandler
from ..utils.logger import Logger
from ..utils.profile import Profile
from ..utils.score import Score
from ..utils.state import State


class TogglManager(object):
    def __init__(self, slackbot=None):
        self.data_handler = DataHandler()
        self.profile = Profile()
        self.logger = Logger().get_logger()

        self.toggl = Toggl()
        self.toggl.setAPIKey(Config.open_api.toggl.TOKEN)

        wid = self.toggl.getWorkspace(name=Config.open_api.toggl.WORKSPACE_NAME)["id"]
        self.toggl.setWorkspaceId(wid)

        if slackbot is None:
            self.slackbot = SlackerAdapter(
                channel=Config.slack.channel.get("TASK", "#general")
            )
        else:
            self.slackbot = slackbot

    def timer(self, description="", doing=True, done=True):
        state = State()
        state.check()

        rest_state = state.current.get(state.REST, {})
        advice_rest_time = rest_state.get("time", None)
        need_focus = any(c for c in ["develop", "research"] if c in description.lower())

        # Advice to you to take a break (work overtime)
        if advice_rest_time is not None:
            is_advice_check = rest_state.get("try", False)
            advice_rest_time = arrow.get(advice_rest_time)
            if need_focus and not is_advice_check \
                    and advice_rest_time > arrow.now():
                state.advice_check()
                self.slackbot.send_message(
                    text=MsgResource.TOGGL_ADVICE_REST(
                        time=advice_rest_time.format("HH:mm")
                    )
                )
                return

        current_timer = self.toggl.currentRunningTimeEntry()["data"]
        if current_timer is None and doing == False:
            self.slackbot.send_message(text=MsgResource.TOGGL_ALREADY_BREAK)
            return

        if current_timer is None:
            if description is None or description == "":
                pid = None
            else:
                # matching name
                lower_description = description.lower()

                TOGGL_DELIMEITER = " - "
                if TOGGL_DELIMEITER in lower_description:
                    name = lower_description.split(" - ")[0]
                    pid = self.__get_pid(name=name)
                else:
                    pid = None

            self.toggl.startTimeEntry(description=description, pid=pid)
            self.slackbot.send_message(text=MsgResource.TOGGL_START)
        else:
            if (doing, done) == (True, False):
                self.slackbot.send_message(text=MsgResource.TOGGL_ALREADY_DOING)
                return

            response = self.toggl.stopTimeEntry(current_timer["id"])["data"]
            self._save_data(response)

            self.slackbot.send_message(text=MsgResource.TOGGL_STOP)

            start_time = response["start"]
            end_time = response["stop"]
            diff_min = ArrowUtil.get_curr_time_diff(
                start=start_time, stop=end_time
            )

            description = response["description"]
            self.slackbot.send_message(
                text=MsgResource.TOGGL_STOP_SUMMARY(
                    description=description, diff_min=diff_min
                )
            )

            if done:
                todoist = TodoistManager()
                todoist.complete_by_toggl(description, int(diff_min))

            state.advice_rest(diff_min)

            if diff_min >= 40:
                attention = AttentionQuestion()
                attention.question()

    def __get_pid(self, name=None):
        project = self.toggl.getWorkspaceProject(name=name)
        if project is None:
            pid = None
        else:
            pid = project["id"]
        return pid

    def _save_data(self, toggl_data):
        data = self._convert_activity_task_format(toggl_data)
        self.data_handler.edit_activity("task", data)

    def _convert_activity_task_format(self, toggl_data):
        entry_id = toggl_data["id"]

        timezone = self.profile.get_timezone()
        start_time = arrow.get(toggl_data["start"]).to(timezone)

        if "end" in toggl_data:
            end_time = toggl_data["end"]
        elif "stop" in toggl_data:
            end_time = toggl_data["stop"]
        else:
            raise ValueError(f"unexpected toggl data format. \n{toggl_data}")
        end_time = arrow.get(end_time).to(timezone)

        project_name = "Empty"
        project_color = "#A9A9A9"
        pid = toggl_data.get("pid", None)
        if pid is not None:
            project = self.toggl.getProject(pid)["data"]
            project_name = project["name"]
            project_color = project["hex_color"]

        description = toggl_data["description"]

        return {
            "toggl_id": entry_id,
            "start_time": start_time.format("YYYY-MM-DDTHH:mm:ssZZ"),
            "end_time": end_time.format("YYYY-MM-DDTHH:mm:ssZZ"),
            "project": project_name,
            "description": description,
            "color": project_color
        }

    def check_toggl_timer(self):
        current_timer = self.toggl.currentRunningTimeEntry()["data"]
        self.logger.info(str(current_timer))
        if current_timer is None:
            return

        diff_min = ArrowUtil.get_curr_time_diff(start=current_timer["start"])
        self.logger.info("diff_min: " + str(diff_min))
        diff_min_divide_10 = int(diff_min / 10)
        if diff_min > 150:
            self.slackbot.send_message(text=MsgResource.TOGGL_NOTI_RELAY)
        else:
            for i in range(3, 10, 3):
                if diff_min_divide_10 == i:
                    self.slackbot.send_message(
                        text=MsgResource.TOGGL_TIMER_CHECK(diff_min=diff_min)
                    )
                    break

    def report(self, kind="chart", timely="weekly"):

        now = arrow.now()

        if timely == "daily":
            before_days = now.shift(days=0)
        elif timely == "weekly":
            before_days = now.shift(days=-6)

        data = {
            "since": before_days.format("YYYY-MM-DD"),
            "until": now.format("YYYY-MM-DD"),
            "calculate": "time",
        }

        channel = Config.slack.channel.get("REPORT", "#general")

        if kind == "basic":
            f_name = "basic-report.pdf"
            self.toggl.getWeeklyReportPDF(data, f_name)
            self.slackbot.file_upload(
                f_name,
                channel=channel,
                title=timely + " 기본 리포트",
                comment=MsgResource.TOGGL_REPORT,
            )
        elif kind == "chart":
            f_name = "chart-report.pdf"
            self.toggl.getSummaryReportPDF(data, f_name)
            self.slackbot.file_upload(
                f_name,
                channel=channel,
                title=timely + " 차트 리포트",
                comment=MsgResource.TOGGL_REPORT,
            )
        elif kind == "detail":
            f_name = "detail-report.pdf"
            self.toggl.getDetailedReportPDF(data, f_name)
            self.slackbot.file_upload(
                f_name,
                channel=channel,
                title=timely + " 상세 리포트",
                comment=MsgResource.TOGGL_REPORT,
            )

    def get_point(self):
        params = self._make_today_params()
        today = self.toggl.getDetailedReport(params)
        if today["total_grand"]:
            total_hours = round(today["total_grand"] / 60 / 60 / 10)
        else:
            total_hours = 0
        return Score.percent(total_hours, 100, 800)

    def sync_task(self):
        params = self._make_today_params()
        detailed_reports = self.toggl.getDetailedReport(params)["data"]
        activity_data = self.data_handler.read_acitivity()

        unadded_tasks = []
        for toggl_data in detailed_reports:
            is_add = False
            for task in activity_data.get("task", []):
                if task["toggl_id"] == toggl_data["id"]:
                    data = self._convert_activity_task_format(toggl_data)
                    task = data
                    is_add = True
                    break

            if not is_add:
                unadded_tasks.append(toggl_data)

        for task in unadded_tasks:
            self._save_data(task)

    def _make_today_params(self):
        now = arrow.now()
        params = {
            "since": now.format("YYYY-MM-DD"),
            "until": now.format("YYYY-MM-DD"),
            "calculate": "time",
        }
        return params
