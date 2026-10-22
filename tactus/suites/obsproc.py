"""Ecflow obs processing suite."""

from pathlib import Path

from tactus.datetime_utils import as_datetime, oi2dt_list
from tactus.os_utils import tactusmakedirs
from tactus.suites.base import EcflowSuiteFamily, EcflowSuiteTask, SuiteDefinition
from tactus.suites.da_components import ObservationFamily
from tactus.logs import logger


class ObsprocSuiteDefinition(SuiteDefinition):
    """Obsprocessing."""

    def __init__(
        self,
        config,
        dry_run=False,
    ):
        """Construct the definition.

        Args:
            config (ParsedConfig): Configuration file
            dry_run (bool, optional): Dry run not using ecflow. Defaults to False.

        Raises:
            ModuleNotFoundError: If ecflow is not loaded and not dry_run

        """
        # Call the base class constructor
        SuiteDefinition.__init__(self, config, dry_run=dry_run)

        unix_group = self.platform.get_platform_value("unix_group")
        tactusmakedirs(self.joboutdir, unixgroup=unix_group)

        # Get the default input template path
        input_template = (
            Path(__file__).parent.resolve() / "../templates/ecflow/default.py"
        )
        input_template = input_template.as_posix()

        prep_run = EcflowSuiteTask(
            "PrepRun",
            self.suite,
            config,
            self.task_settings,
            self.ecf_files,
            input_template=input_template,
            ecf_files_remotely=self.ecf_files_remotely,
        )

        obs_trigger = []
        config_basetime = as_datetime(config["general.times.basetime"])
        dt_list = oi2dt_list(
            config["general.output_settings.history"],
            config["general.times.forecast_range"],
        )

        logger.info(config_basetime)
        for dt in dt_list:
            logger.info(dt)
            basetime = config_basetime + dt
            basetime_str = basetime.strftime("%Y-%m-%dT%H:%M:%SZ")
            time_variables = {
                "BASETIME": basetime_str,
                "VALIDTIME": basetime_str,
            }

            date_family = EcflowSuiteFamily(
                format_time(basetime_str),
                self.suite,
                self.ecf_files,
                variables=time_variables,
                ecf_files_remotely=self.ecf_files_remotely,
            )
            for da_stream in ("surface", "upper_air"):
                if config.get(f"da.{da_stream}.active", False):
                    obs_family = ObservationFamily(
                        date_family,
                        config,
                        self.task_settings,
                        input_template,
                        self.ecf_files,
                        trigger=prep_run,
                        da_stream=da_stream,
                        ecf_files_remotely=self.ecf_files_remotely,
                    )
                    obs_trigger.append(obs_family)

        if config["suite_control.do_cleaning"]:
            EcflowSuiteTask(
                "PostMortem",
                self.suite,
                config,
                self.task_settings,
                self.ecf_files,
                input_template=input_template,
                trigger=obs_trigger,
                variables={
                    "TACTUS_TASK": "Cleaning",
                    "ARGS": "cleaning_type=PostMortem",
                },
                ecf_files_remotely=self.ecf_files_remotely,
            )

def format_time(t):
    return t.replace("+00:00", "Z").replace("-", "").replace(":", "_")


