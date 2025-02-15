# Copyright (C) 2019-2022 Intel Corporation
# Copyright (C) 2023-2024 CVAT.ai Corporation
#
# SPDX-License-Identifier: MIT

import logging
import os
import os.path as osp
import tempfile
from datetime import timedelta

import django_rq
import rq
from os.path import exists as osp_exists
from django.conf import settings
from django.utils import timezone
from rq_scheduler import Scheduler

import cvat.apps.dataset_manager.project as project
import cvat.apps.dataset_manager.task as task
from cvat.apps.engine.log import ServerLogManager
from cvat.apps.engine.models import Job, Project, Task
from cvat.apps.engine.utils import get_rq_lock_by_user
from cvat.apps.engine.rq_job_handler import RQMeta

from .formats.registry import EXPORT_FORMATS, IMPORT_FORMATS
from .util import (
    LockNotAvailableError,
    current_function_name, get_export_cache_lock,
    get_export_cache_dir, make_export_filename,
    parse_export_file_path, extend_export_file_lifetime
)
from .util import EXPORT_CACHE_DIR_NAME  # pylint: disable=unused-import


slogger = ServerLogManager(__name__)

_MODULE_NAME = __package__ + '.' + osp.splitext(osp.basename(__file__))[0]

def log_exception(logger: logging.Logger | None = None, exc_info: bool = True):
    if logger is None:
        logger = slogger.glob
    logger.exception("[%s @ %s]: exception occurred" % \
            (_MODULE_NAME, current_function_name(2)),
        exc_info=exc_info)

DEFAULT_CACHE_TTL = timedelta(seconds=settings.EXPORT_CACHE_TTL)
PROJECT_CACHE_TTL = DEFAULT_CACHE_TTL
TASK_CACHE_TTL = DEFAULT_CACHE_TTL
JOB_CACHE_TTL = DEFAULT_CACHE_TTL
TTL_CONSTS = {
    'project': PROJECT_CACHE_TTL,
    'task': TASK_CACHE_TTL,
    'job': JOB_CACHE_TTL,
}

EXPORT_CACHE_LOCK_ACQUISITION_TIMEOUT = timedelta(seconds=settings.EXPORT_CACHE_LOCK_ACQUISITION_TIMEOUT)
EXPORT_CACHE_LOCK_TTL = timedelta(seconds=settings.EXPORT_CACHE_LOCK_TTL)
EXPORT_LOCKED_RETRY_INTERVAL = timedelta(seconds=settings.EXPORT_LOCKED_RETRY_INTERVAL)


def get_export_cache_ttl(db_instance: str | Project | Task | Job) -> timedelta:
    if isinstance(db_instance, (Project, Task, Job)):
        db_instance = db_instance.__class__.__name__

    return TTL_CONSTS[db_instance.lower()]

def _patch_scheduled_job_status(job: rq.job.Job):
    # NOTE: rq scheduler < 0.14 does not set the appropriate
    # job status (SCHEDULED). This has been fixed in the 0.14 version.
    # https://github.com/rq/rq-scheduler/blob/f7d5787c5f94b5517e209c612ef648f4bfc44f9e/rq_scheduler/scheduler.py#L148
    # FUTURE-TODO: delete manual status setting after upgrading to 0.14
    if job.get_status(refresh=False) != rq.job.JobStatus.SCHEDULED:
        job.set_status(rq.job.JobStatus.SCHEDULED)

def _retry_current_rq_job(time_delta: timedelta) -> rq.job.Job:
    # TODO: implement using retries once we move from rq_scheduler to builtin RQ scheduler
    # for better reliability and error reporting

    # This implementation can potentially lead to 2 jobs with the same name running in parallel,
    # if the retry is enqueued immediately.
    assert time_delta.total_seconds() > 0

    current_rq_job = rq.get_current_job()

    def _patched_retry(*_1, **_2):
        scheduler: Scheduler = django_rq.get_scheduler(
            settings.CVAT_QUEUES.EXPORT_DATA.value
        )

        user_id = current_rq_job.meta.get('user', {}).get('id') or -1

        with get_rq_lock_by_user(settings.CVAT_QUEUES.EXPORT_DATA.value, user_id):
            scheduled_rq_job: rq.job.Job = scheduler.enqueue_in(
                time_delta,
                current_rq_job.func,
                *current_rq_job.args,
                **current_rq_job.kwargs,
                job_id=current_rq_job.id,
                meta=RQMeta.reset_meta_on_retry(current_rq_job.meta),
                job_ttl=current_rq_job.ttl,
                job_result_ttl=current_rq_job.result_ttl,
                job_description=current_rq_job.description,
                on_success=current_rq_job.success_callback,
                on_failure=current_rq_job.failure_callback,
            )
            _patch_scheduled_job_status(scheduled_rq_job)

    current_rq_job.retries_left = 1
    setattr(current_rq_job, 'retry', _patched_retry)
    return current_rq_job

def export(
    *,
    dst_format: str,
    project_id: int | None = None,
    task_id: int | None = None,
    job_id: int | None = None,
    server_url: str | None = None,
    save_images: bool = False,
):
    try:
        if task_id is not None:
            logger = slogger.task[task_id]
            export_fn = task.export_task
            db_instance = Task.objects.get(pk=task_id)
        elif project_id is not None:
            logger = slogger.project[project_id]
            export_fn = project.export_project
            db_instance = Project.objects.get(pk=project_id)
        else:
            logger = slogger.job[job_id]
            export_fn = task.export_job
            db_instance = Job.objects.get(pk=job_id)

        cache_ttl = get_export_cache_ttl(db_instance)

        cache_dir = get_export_cache_dir(db_instance)

        # As we're not locking the db object here, it can be updated by the time of actual export.
        # The file will be saved with the older timestamp.
        # When it's time to download the file, it will be handled - the export will be restarted.
        # The situation is considered rare, so no locking is used.
        instance_update_time = timezone.localtime(db_instance.updated_date)
        if isinstance(db_instance, Project):
            tasks_update = list(map(
                lambda db_task: timezone.localtime(db_task.updated_date),
                db_instance.tasks.all()
            ))
            instance_update_time = max(tasks_update + [instance_update_time])

        output_path = make_export_filename(
            cache_dir, save_images, instance_update_time.timestamp(), dst_format
        )

        os.makedirs(cache_dir, exist_ok=True)

        # acquire a lock 2 times instead of using one long lock:
        # 1. to check whether the file exists or not
        # 2. to create a file when it doesn't exist
        with get_export_cache_lock(
            output_path,
            ttl=EXPORT_CACHE_LOCK_TTL,
            acquire_timeout=EXPORT_CACHE_LOCK_ACQUISITION_TIMEOUT,
        ):
            if osp_exists(output_path):
                extend_export_file_lifetime(output_path)
                return output_path

        with tempfile.TemporaryDirectory(dir=cache_dir) as temp_dir:
            temp_file = osp.join(temp_dir, 'result')
            export_fn(db_instance.id, temp_file, dst_format,
                server_url=server_url, save_images=save_images)
            with get_export_cache_lock(
                output_path,
                ttl=EXPORT_CACHE_LOCK_TTL,
                acquire_timeout=EXPORT_CACHE_LOCK_ACQUISITION_TIMEOUT,
            ):
                os.replace(temp_file, output_path)

        scheduler: Scheduler = django_rq.get_scheduler(settings.CVAT_QUEUES.EXPORT_DATA.value)
        cleaning_job = scheduler.enqueue_in(
            time_delta=cache_ttl,
            func=clear_export_cache,
            file_path=output_path,
            file_ctime=instance_update_time.timestamp(),
            logger=logger,
        )
        _patch_scheduled_job_status(cleaning_job)
        logger.info(
            "The {} '{}' is exported as '{}' at '{}' "
            "and available for downloading for the next {}. "
            "Export cache cleaning job is enqueued, id '{}'".format(
                db_instance.__class__.__name__.lower(),
                db_instance.id,
                dst_format,
                output_path,
                cache_ttl,
                cleaning_job.id,
            )
        )

        return output_path
    except LockNotAvailableError:
        # Need to retry later if the lock was not available
        _retry_current_rq_job(EXPORT_LOCKED_RETRY_INTERVAL)
        logger.info(
            "Failed to acquire export cache lock. Retrying in {}".format(
                EXPORT_LOCKED_RETRY_INTERVAL
            )
        )
        raise
    except Exception:
        log_exception(logger)
        raise

def export_job_annotations(job_id: int, dst_format: str, *, server_url: str | None = None):
    return export(dst_format=dst_format, job_id=job_id, server_url=server_url, save_images=False)

def export_job_as_dataset(job_id: int, dst_format: str, *, server_url: str | None = None):
    return export(dst_format=dst_format, job_id=job_id, server_url=server_url, save_images=True)

def export_task_as_dataset(task_id: int, dst_format: str, *, server_url: str | None = None):
    return export(dst_format=dst_format, task_id=task_id, server_url=server_url, save_images=True)

def export_task_annotations(task_id: int, dst_format: str, *, server_url: str | None = None):
    return export(dst_format=dst_format, task_id=task_id, server_url=server_url, save_images=False)

def export_project_as_dataset(project_id: int, dst_format: str, *, server_url: str | None = None):
    return export(dst_format=dst_format, project_id=project_id, server_url=server_url, save_images=True)

def export_project_annotations(project_id: int, dst_format: str, *, server_url: str | None = None):
    return export(dst_format=dst_format, project_id=project_id, server_url=server_url, save_images=False)


class FileIsBeingUsedError(Exception):
    pass

def clear_export_cache(file_path: str, file_ctime: float, logger: logging.Logger) -> None:
    # file_ctime is for backward compatibility with older RQ jobs, not needed now

    try:
        with get_export_cache_lock(
            file_path,
            block=True,
            acquire_timeout=EXPORT_CACHE_LOCK_ACQUISITION_TIMEOUT,
            ttl=EXPORT_CACHE_LOCK_TTL,
        ):
            if not osp.exists(file_path):
                raise FileNotFoundError("Export cache file '{}' doesn't exist".format(file_path))

            parsed_filename = parse_export_file_path(file_path)
            cache_ttl = get_export_cache_ttl(parsed_filename.instance_type)

            if timezone.now().timestamp() <= osp.getmtime(file_path) + cache_ttl.total_seconds():
                # Need to retry later, the export is in use
                _retry_current_rq_job(cache_ttl)
                logger.info(
                    "Export cache file '{}' is recently accessed, will retry in {}".format(
                        file_path, cache_ttl
                    )
                )
                raise FileIsBeingUsedError # should be handled by the worker

            # TODO: maybe remove all outdated exports
            os.remove(file_path)
            logger.info("Export cache file '{}' successfully removed".format(file_path))
    except LockNotAvailableError:
        # Need to retry later if the lock was not available
        _retry_current_rq_job(EXPORT_LOCKED_RETRY_INTERVAL)
        logger.info(
            "Failed to acquire export cache lock. Retrying in {}".format(
                EXPORT_LOCKED_RETRY_INTERVAL
            )
        )
        raise
    except Exception:
        log_exception(logger)
        raise

def get_export_formats():
    return list(EXPORT_FORMATS.values())

def get_import_formats():
    return list(IMPORT_FORMATS.values())

def get_all_formats():
    return {
        'importers': get_import_formats(),
        'exporters': get_export_formats(),
    }
