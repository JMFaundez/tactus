"""Ecflow Jb downscaling suite.
"""

from pathlib import Path

from tactus.datetime_utils import as_datetime
from tactus.os_utils import tactusmakedirs
from tactus.suites.base import (
    EcflowSuiteTask,
    SuiteDefinition,
)
from tactus.suites.tactus_suite_components import (
    StaticDataFamily,
    TimeDependentFamily,
)


class JbdownSuiteDefinition(SuiteDefinition):
    """Jb downscaling: the tactus campaign plus FESTAT."""

    def __init__(self, config, dry_run=False):
        """Construct the definition.

        Args:
            config (ParsedConfig): Configuration file
            dry_run (bool, optional): Dry run not using ecflow. Defaults to False.

        """
        SuiteDefinition.__init__(self, config, dry_run=dry_run)
        unix_group = self.platform.get_platform_value("unix_group")
        tactusmakedirs(self.joboutdir, unixgroup=unix_group)

        input_template = (
            Path(__file__).parent.resolve() / "../templates/ecflow/default.py"
        ).as_posix()

        max_ecf_tasks = self.config.get("submission.max_ecf_tasks", -1)
        if max_ecf_tasks > 0 and self.suite.ecf_node is not None:
            self.suite.ecf_node.add_limit("max_ecf_tasks", max_ecf_tasks)
            self.suite.ecf_node.add_inlimit("max_ecf_tasks", f"/{self.name}")

        self.do_prep = config["general.surfex"]
        create_static_data = config["suite_control.create_static_data"]
        if config["suite_control.mode"] == "restart":
            self.do_prep = False
            create_static_data = False

        final_trigger = []
        time_dependent_trigger_node = None

        prep_run = EcflowSuiteTask(
            "PrepRun",
            self.suite,
            config,
            self.task_settings,
            self.ecf_files,
            input_template=input_template,
            ecf_files_remotely=self.ecf_files_remotely,
        )

        if config["suite_control.do_cleaning"]:
            final_trigger.append(prep_run)
            time_dependent_trigger_node = prep_run

        static_data = None
        if create_static_data:
            static_data = StaticDataFamily(
                self.suite,
                config,
                self.task_settings,
                input_template,
                self.ecf_files,
                trigger=prep_run,
                ecf_files_remotely=self.ecf_files_remotely,
                dry_run=dry_run,
            )
            # The campaign and Festat wait for the static data
            time_dependent_trigger_node = static_data
            collect_logs_trigger = [static_data]

            if config["suite_control.do_archiving"]:
                archive_static = EcflowSuiteTask(
                    "ArchiveStatic",
                    self.suite,
                    config,
                    self.task_settings,
                    self.ecf_files,
                    input_template=input_template,
                    variables=None,
                    trigger=static_data,
                )
                collect_logs_trigger.append(archive_static)

            collect_logs = EcflowSuiteTask(
                "CollectLogsStatic",
                self.suite,
                config,
                self.task_settings,
                self.ecf_files,
                input_template=input_template,
                variables=None,
                trigger=collect_logs_trigger,
                ecf_files_remotely=self.ecf_files_remotely,
            )
            final_trigger.append(collect_logs)

        festat_trigger = static_data if static_data is not None else prep_run
        if config["suite_control.create_time_dependent_suite"]:
            time_dependent_family = TimeDependentFamily(
                self.suite,
                config,
                self.task_settings,
                input_template,
                self.ecf_files,
                time_dependent_trigger_node,
                ecf_files_remotely=self.ecf_files_remotely,
                do_prep=self.do_prep,
                dry_run=dry_run,
            )
            festat_trigger = time_dependent_family.last_node
            final_trigger.append(time_dependent_family.last_node)

        festat = None
        if config.get("suite_control.use_festat", False):
            start = as_datetime(config["general.times.start"])
            festat = EcflowSuiteTask(
                "Festat",
                self.suite,
                config,
                self.task_settings,
                self.ecf_files,
                input_template=input_template,
                trigger=festat_trigger,
                variables={
                    "ARGS": f"dtgbeg={start.strftime('%Y-%m-%dT%H:%M:%SZ')}"
                },
                ecf_files_remotely=self.ecf_files_remotely,
            )

        if config["suite_control.do_cleaning"]:
            EcflowSuiteTask(
                "PostMortem",
                self.suite,
                config,
                self.task_settings,
                self.ecf_files,
                input_template=input_template,
                trigger=festat if festat is not None else final_trigger,
                variables={
                    "TACTUS_TASK": "Cleaning",
                    "ARGS": "cleaning_type=PostMortem",
                },
                ecf_files_remotely=self.ecf_files_remotely,
            )