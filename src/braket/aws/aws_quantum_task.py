# Copyright 2019-2019 Amazon.com, Inc. or its affiliates. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License"). You
# may not use this file except in compliance with the License. A copy of
# the License is located at
#
#     http://aws.amazon.com/apache2.0/
#
# or in the "license" file accompanying this file. This file is
# distributed on an "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF
# ANY KIND, either express or implied. See the License for the specific
# language governing permissions and limitations under the License.

from __future__ import annotations

import asyncio
import time
from functools import singledispatch
from logging import Logger, getLogger
from typing import Any, Dict, Union

import boto3

from braket.annealing.problem import Problem
from braket.aws.aws_session import AwsSession
from braket.circuits.circuit import Circuit
from braket.circuits.circuit_helpers import validate_circuit_and_shots
from braket.device_schema import GateModelParameters
from braket.device_schema.dwave import DwaveDeviceParameters
from braket.device_schema.ionq import IonqDeviceParameters
from braket.device_schema.rigetti import RigettiDeviceParameters
from braket.device_schema.simulators import GateModelSimulatorDeviceParameters
from braket.schema_common import BraketSchemaBase
from braket.task_result import AnnealingTaskResult, GateModelTaskResult
from braket.tasks import AnnealingQuantumTaskResult, GateModelQuantumTaskResult, QuantumTask


class AwsQuantumTask(QuantumTask):
    """Amazon Braket implementation of a quantum task. A task can be a circuit or an annealing
    problem."""

    # TODO: Add API documentation that defines these states. Make it clear this is the contract.
    NO_RESULT_TERMINAL_STATES = {"FAILED", "CANCELLED"}
    RESULTS_READY_STATES = {"COMPLETED"}

    DEFAULT_RESULTS_POLL_TIMEOUT = 120
    DEFAULT_RESULTS_POLL_INTERVAL = 0.25
    RESULTS_FILENAME = "results.json"

    @staticmethod
    def create(
        aws_session: AwsSession,
        device_arn: str,
        task_specification: Union[Circuit, Problem],
        s3_destination_folder: AwsSession.S3DestinationFolder,
        shots: int,
        device_parameters: Dict[str, Any] = None,
        *args,
        **kwargs,
    ) -> AwsQuantumTask:
        """AwsQuantumTask factory method that serializes a quantum task specification
        (either a quantum circuit or annealing problem), submits it to Amazon Braket,
        and returns back an AwsQuantumTask tracking the execution.

        Args:
            aws_session (AwsSession): AwsSession to connect to AWS with.

            device_arn (str): The ARN of the quantum device.

            task_specification (Union[Circuit, Problem]): The specification of the task
                to run on device.

            s3_destination_folder (AwsSession.S3DestinationFolder): NamedTuple, with bucket
                for index 0 and key for index 1, that specifies the Amazon S3 bucket and folder
                to store task results in.

            shots (int): The number of times to run the task on the device. If the device is a
                simulator, this implies the state is sampled N times, where N = `shots`.
                `shots=0` is only available on simulators and means that the simulator
                will compute the exact results based on the task specification.

            device_parameters (Dict[str, Any]): Additional parameters to send to the device.
                For example, for D-Wave:
                `{"providerLevelParameters": {"postprocessingType": "OPTIMIZATION"}}`

        Returns:
            AwsQuantumTask: AwsQuantumTask tracking the task execution on the device.

        Note:
            The following arguments are typically defined via clients of Device.
                - `task_specification`
                - `s3_destination_folder`
                - `shots`

        See Also:
            `braket.aws.aws_quantum_simulator.AwsQuantumSimulator.run()`
            `braket.aws.aws_qpu.AwsQpu.run()`
        """
        if len(s3_destination_folder) != 2:
            raise ValueError(
                "s3_destination_folder must be of size 2 with a 'bucket' and 'key' respectively."
            )

        create_task_kwargs = _create_common_params(
            device_arn,
            s3_destination_folder,
            shots if shots is not None else AwsQuantumTask.DEFAULT_SHOTS,
        )
        return _create_internal(
            task_specification,
            aws_session,
            create_task_kwargs,
            device_parameters or {},
            device_arn,
            *args,
            **kwargs,
        )

    def __init__(
        self,
        arn: str,
        aws_session: AwsSession = None,
        poll_timeout_seconds: int = DEFAULT_RESULTS_POLL_TIMEOUT,
        poll_interval_seconds: int = DEFAULT_RESULTS_POLL_INTERVAL,
        logger: Logger = getLogger(__name__),
    ):
        """
        Args:
            arn (str): The ARN of the task.
            aws_session (AwsSession, optional): The `AwsSession` for connecting to AWS services.
                Default is `None`, in which case an `AwsSession` object will be created with the
                region of the task.
            poll_timeout_seconds (int): The polling timeout for result(), default is 120 seconds.
            poll_interval_seconds (int): The polling interval for result(), default is 0.25
                seconds.
            logger (Logger): Logger object with which to write logs, such as task statuses
                while waiting for task to be in a terminal state. Default is `getLogger(__name__)`

        Examples:
            >>> task = AwsQuantumTask(arn='task_arn')
            >>> task.state()
            'COMPLETED'
            >>> result = task.result()
            AnnealingQuantumTaskResult(...)

            >>> task = AwsQuantumTask(arn='task_arn', poll_timeout_seconds=300)
            >>> result = task.result()
            GateModelQuantumTaskResult(...)
        """

        self._arn: str = arn
        self._aws_session: AwsSession = aws_session or AwsQuantumTask._aws_session_for_task_arn(
            task_arn=arn
        )
        self._poll_timeout_seconds = poll_timeout_seconds
        self._poll_interval_seconds = poll_interval_seconds
        self._logger = logger

        self._metadata: Dict[str, Any] = {}
        self._result: Union[GateModelQuantumTaskResult, AnnealingQuantumTaskResult] = None

    @staticmethod
    def _aws_session_for_task_arn(task_arn: str) -> AwsSession:
        """
        Get an AwsSession for the Task ARN. The AWS session should be in the region of the task.

        Returns:
            AwsSession: `AwsSession` object with default `boto_session` in task's region
        """
        task_region = task_arn.split(":")[3]
        boto_session = boto3.Session(region_name=task_region)
        return AwsSession(boto_session=boto_session)

    @property
    def id(self) -> str:
        """str: The ARN of the quantum task."""
        return self._arn

    def _cancel_future(self) -> None:
        """Cancel the future if it exists. Else, create a cancelled future."""
        if hasattr(self, "_future"):
            self._future.cancel()
        else:
            self._future = asyncio.Future()
            self._future.cancel()

    def cancel(self) -> None:
        """Cancel the quantum task. This cancels the future and the task in Amazon Braket."""
        self._cancel_future()
        self._aws_session.cancel_quantum_task(self._arn)

    def metadata(self, use_cached_value: bool = False) -> Dict[str, Any]:
        """
        Get task metadata defined in Amazon Braket.

        Args:
            use_cached_value (bool, optional): If `True`, uses the value most recently retrieved
                from the Amazon Braket `GetQuantumTask` operation. If `False`, calls the
                `GetQuantumTask` operation  to retrieve metadata, which also updates the cached
                value. Default = `False`.
        Returns:
            Dict[str, Any]: The response from the Amazon Braket `GetQuantumTask` operation.
            If `use_cached_value` is `True`, Amazon Braket is not called and the most recently
            retrieved value is used.
        """
        if not use_cached_value:
            self._metadata = self._aws_session.get_quantum_task(self._arn)
        return self._metadata

    def state(self, use_cached_value: bool = False) -> str:
        """
        The state of the quantum task.

        Args:
            use_cached_value (bool, optional): If `True`, uses the value most recently retrieved
                from the Amazon Braket `GetQuantumTask` operation. If `False`, calls the
                `GetQuantumTask` operation to retrieve metadata, which also updates the cached
                value. Default = `False`.
        Returns:
            str: The value of `status` in `metadata()`. This is the value of the `status` key
            in the Amazon Braket `GetQuantumTask` operation. If `use_cached_value` is `True`,
            the value most recently returned from the `GetQuantumTask` operation is used.
        See Also:
            `metadata()`
        """
        return self.metadata(use_cached_value).get("status")

    def result(self) -> Union[GateModelQuantumTaskResult, AnnealingQuantumTaskResult]:
        """
        Get the quantum task result by polling Amazon Braket to see if the task is completed.
        Once the task is completed, the result is retrieved from S3 and returned as a
        `GateModelQuantumTaskResult` or `AnnealingQuantumTaskResult`

        This method is a blocking thread call and synchronously returns a result. Call
        async_result() if you require an asynchronous invocation.
        Consecutive calls to this method return a cached result from the preceding request.
        """
        try:
            return asyncio.get_event_loop().run_until_complete(self.async_result())
        except asyncio.CancelledError:
            # Future was cancelled, return whatever is in self._result if anything
            self._logger.warning("Task future was cancelled")
            return self._result

    def _get_future(self):
        try:
            asyncio.get_event_loop()
        except Exception as e:
            self._logger.debug(e)
            self._logger.info("No event loop found; creating new event loop")
            asyncio.set_event_loop(asyncio.new_event_loop())

        if not hasattr(self, "_future"):
            self._future = asyncio.get_event_loop().run_until_complete(self._create_future())
        elif (
            self._future.done() and not self._future.cancelled() and self._result is None
        ):  # timed out and no result
            task_status = self.metadata()["status"]
            if task_status in self.NO_RESULT_TERMINAL_STATES:
                self._logger.warning(
                    f"Task is in terminal state {task_status} and no result is available"
                )
            else:
                self._future = asyncio.get_event_loop().run_until_complete(self._create_future())
        return self._future

    def async_result(self) -> asyncio.Task:
        """
        Get the quantum task result asynchronously. Consecutive calls to this method return
        the result cached from the most recent request.
        """
        return self._get_future()

    async def _create_future(self) -> asyncio.Task:
        """
        Wrap the `_wait_for_completion` coroutine inside a future-like object.
        Invoking this method starts the coroutine and returns back the future-like object
        that contains it. Note that this does not block on the coroutine to finish.

        Returns:
            asyncio.Task: An asyncio Task that contains the _wait_for_completion() coroutine.
        """
        return asyncio.create_task(self._wait_for_completion())

    async def _wait_for_completion(
        self,
    ) -> Union[GateModelQuantumTaskResult, AnnealingQuantumTaskResult]:
        """
        Waits for the quantum task to be completed, then returns the result from the S3 bucket.

        Returns:
            Union[GateModelQuantumTaskResult, AnnealingQuantumTaskResult]: If the task is in the
                `AwsQuantumTask.RESULTS_READY_STATES` state within the specified time limit,
                the result from the S3 bucket is loaded and returned.
                `None` is returned if a timeout occurs or task state is in
                `AwsQuantumTask.NO_RESULT_TERMINAL_STATES`.
        Note:
            Timeout and sleep intervals are defined in the constructor fields
                `poll_timeout_seconds` and `poll_interval_seconds` respectively.
        """
        self._logger.debug(f"Task {self._arn}: start polling for completion")
        start_time = time.time()

        while (time.time() - start_time) < self._poll_timeout_seconds:
            current_metadata = self.metadata()
            task_status = current_metadata["status"]
            self._logger.debug(f"Task {self._arn}: task status {task_status}")
            if task_status in AwsQuantumTask.RESULTS_READY_STATES:
                result_string = self._aws_session.retrieve_s3_object_body(
                    current_metadata["outputS3Bucket"],
                    current_metadata["outputS3Directory"] + f"/{AwsQuantumTask.RESULTS_FILENAME}",
                )
                print(f"Result string is \n {result_string}")
                self._result = _format_result(BraketSchemaBase.parse_raw_schema(result_string))
                return self._result
            elif task_status in AwsQuantumTask.NO_RESULT_TERMINAL_STATES:
                self._logger.warning(
                    f"Task is in terminal state {task_status} and no result is available"
                )
                self._result = None
                return None
            else:
                await asyncio.sleep(self._poll_interval_seconds)

        # Timed out
        self._logger.warning(
            f"Task {self._arn}: polling for task completion timed out after "
            + f"{time.time()-start_time} seconds. Please increase the timeout; "
            + "this can be done by creating a new AwsQuantumTask with this task's ARN "
            + "and a higher value for the `poll_timeout_seconds` parameter."
        )
        self._result = None
        return None

    def __repr__(self) -> str:
        return f"AwsQuantumTask('id':{self.id})"

    def __eq__(self, other) -> bool:
        if isinstance(other, AwsQuantumTask):
            return self.id == other.id
        return NotImplemented

    def __hash__(self) -> int:
        return hash(self.id)


@singledispatch
def _create_internal(
    task_specification: Union[Circuit, Problem],
    aws_session: AwsSession,
    create_task_kwargs: Dict[str, Any],
    device_parameters: Union[dict, BraketSchemaBase],
    device_arn: str,
    *args,
    **kwargs,
) -> AwsQuantumTask:
    raise TypeError("Invalid task specification type")


@_create_internal.register
def _(
    circuit: Circuit,
    aws_session: AwsSession,
    create_task_kwargs: Dict[str, Any],
    device_parameters: Union[dict, BraketSchemaBase],
    device_arn: str,
    *args,
    **kwargs,
) -> AwsQuantumTask:
    validate_circuit_and_shots(circuit, create_task_kwargs["shots"])

    # TODO: Update this to use `deviceCapabilities` from Amazon Braket's GetDevice operation
    # in order to decide what parameters to build.
    paradigm_parameters = GateModelParameters(qubitCount=circuit.qubit_count)
    if "ionq" in device_arn:
        device_parameters = IonqDeviceParameters(paradigmParameters=paradigm_parameters)
    elif "rigetti" in device_arn:
        device_parameters = RigettiDeviceParameters(paradigmParameters=paradigm_parameters)
    else:  # default to use simulator
        device_parameters = GateModelSimulatorDeviceParameters(
            paradigmParameters=paradigm_parameters
        )

    create_task_kwargs.update(
        {"action": circuit.to_ir().json(), "deviceParameters": device_parameters.json()}
    )
    task_arn = aws_session.create_quantum_task(**create_task_kwargs)
    return AwsQuantumTask(task_arn, aws_session, *args, **kwargs)


@_create_internal.register
def _(
    problem: Problem,
    aws_session: AwsSession,
    create_task_kwargs: Dict[str, Any],
    device_parameters: Union[dict, DwaveDeviceParameters],
    device_arn: str,
    *args,
    **kwargs,
) -> AwsQuantumTask:
    create_task_kwargs.update(
        {
            "action": problem.to_ir().json(),
            "deviceParameters": DwaveDeviceParameters.parse_obj(device_parameters).json(),
        }
    )

    task_arn = aws_session.create_quantum_task(**create_task_kwargs)
    return AwsQuantumTask(task_arn, aws_session, *args, **kwargs)


def _create_common_params(
    device_arn: str, s3_destination_folder: AwsSession.S3DestinationFolder, shots: int
) -> Dict[str, Any]:
    return {
        "deviceArn": device_arn,
        "outputS3Bucket": s3_destination_folder[0],
        "outputS3KeyPrefix": s3_destination_folder[1],
        "shots": shots,
    }


@singledispatch
def _format_result(result):
    raise TypeError("Invalid result specification type")


@_format_result.register
def _(result: GateModelTaskResult) -> GateModelQuantumTaskResult:
    if result.resultTypes:
        for result_type in result.resultTypes:
            type = result_type.type.type
            if type == "amplitude":
                for state in result_type.value:
                    result_type.value[state] = complex(*result_type.value[state])
    return GateModelQuantumTaskResult.from_object(result)


@_format_result.register
def _(result: AnnealingTaskResult) -> AnnealingQuantumTaskResult:
    return AnnealingQuantumTaskResult.from_object(result)
