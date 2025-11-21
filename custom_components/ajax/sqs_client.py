"""AWS SQS client for Ajax real-time events."""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Callable

try:
    import aiobotocore.session
    from botocore.exceptions import ClientError
    HAS_AIOBOTOCORE = True
except ImportError:
    HAS_AIOBOTOCORE = False

_LOGGER = logging.getLogger(__name__)


class SQSClientError(Exception):
    """Base exception for SQS client errors."""


class AjaxSQSClient:
    """AWS SQS client for receiving Ajax events."""

    def __init__(
        self,
        aws_access_key: str,
        aws_secret_key: str,
        queue_name: str,
        region: str = "eu-central-1",
    ):
        """Initialize the SQS client.

        Args:
            aws_access_key: AWS access key ID
            aws_secret_key: AWS secret access key
            queue_name: SQS queue name (e.g., 'ajax-events-XXXXXXXX-prod.fifo')
            region: AWS region (default: eu-central-1 for Frankfurt)
        """
        if not HAS_AIOBOTOCORE:
            raise SQSClientError(
                "aiobotocore is required for SQS support. "
                "Install with: pip install aiobotocore"
            )

        self.aws_access_key = aws_access_key
        self.aws_secret_key = aws_secret_key
        self.queue_name = queue_name
        self.region = region

        self._running = False
        self._task: asyncio.Task | None = None
        self._event_callback: Callable[[dict[str, Any]], None] | None = None

    def set_event_callback(self, callback: Callable[[dict[str, Any]], None]):
        """Set callback function for incoming events.

        Args:
            callback: Async function to call with event data
        """
        self._event_callback = callback

    async def start(self):
        """Start listening to SQS queue."""
        if self._running:
            _LOGGER.warning("SQS listener already running")
            return

        if not self._event_callback:
            raise SQSClientError("Event callback not set. Call set_event_callback() first.")

        self._running = True
        self._task = asyncio.create_task(self._listen_loop())
        _LOGGER.info("Started SQS listener for queue: %s", self.queue_name)

    async def stop(self):
        """Stop listening to SQS queue."""
        self._running = False

        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

        _LOGGER.info("Stopped SQS listener")

    async def _get_queue_url(self, sqs_client) -> str:
        """Get queue URL from queue name.

        Args:
            sqs_client: Boto3 SQS client

        Returns:
            Queue URL

        Raises:
            SQSClientError: If queue not found
        """
        try:
            response = await sqs_client.get_queue_url(QueueName=self.queue_name)
            return response["QueueUrl"]
        except ClientError as err:
            error_code = err.response.get("Error", {}).get("Code", "Unknown")
            if error_code == "AWS.SimpleQueueService.NonExistentQueue":
                raise SQSClientError(
                    f"Queue '{self.queue_name}' not found. "
                    f"Please verify the queue name with Ajax Systems."
                ) from err
            _LOGGER.error("Failed to get queue URL: %s", err)
            raise SQSClientError(f"Failed to get queue URL: {err}") from err

    async def _listen_loop(self):
        """Main listening loop for SQS messages."""
        session = aiobotocore.session.get_session()

        while self._running:
            try:
                async with session.create_client(
                    "sqs",
                    region_name=self.region,
                    aws_access_key_id=self.aws_access_key,
                    aws_secret_access_key=self.aws_secret_key,
                ) as sqs_client:

                    queue_url = await self._get_queue_url(sqs_client)
                    _LOGGER.debug("Listening to queue: %s", queue_url)

                    while self._running:
                        try:
                            # Long polling (wait up to 20 seconds for messages)
                            response = await sqs_client.receive_message(
                                QueueUrl=queue_url,
                                MaxNumberOfMessages=10,  # Process up to 10 messages at once
                                WaitTimeSeconds=20,  # Long polling
                                AttributeNames=["All"],
                                MessageAttributeNames=["All"],
                            )

                            messages = response.get("Messages", [])

                            if messages:
                                _LOGGER.debug("Received %d message(s) from SQS", len(messages))

                                for message in messages:
                                    await self._process_message(message, sqs_client, queue_url)
                            else:
                                _LOGGER.debug("No messages, continuing long polling...")

                        except ClientError as err:
                            _LOGGER.error("Error receiving messages: %s", err)
                            await asyncio.sleep(5)
                        except Exception as err:
                            _LOGGER.exception("Unexpected error in receive loop: %s", err)
                            await asyncio.sleep(5)

            except Exception as err:
                _LOGGER.error("SQS client error: %s", err)
                if self._running:
                    _LOGGER.info("Reconnecting in 10 seconds...")
                    await asyncio.sleep(10)

    async def _process_message(
        self,
        message: dict[str, Any],
        sqs_client,
        queue_url: str,
    ):
        """Process a single SQS message.

        Args:
            message: SQS message
            sqs_client: Boto3 SQS client
            queue_url: Queue URL
        """
        try:
            # Parse message body
            body = json.loads(message["Body"])

            # Handle SNS wrapper if present
            # (Ajax may wrap events in SNS notifications)
            if "Message" in body:
                event_data = json.loads(body["Message"])
            else:
                event_data = body

            event_type = event_data.get("type", "unknown")
            _LOGGER.debug("Processing event: %s", event_type)

            # Call the event callback
            if self._event_callback:
                try:
                    await self._event_callback(event_data)
                except Exception as err:
                    _LOGGER.error("Error in event callback: %s", err)

            # Delete message from queue after successful processing
            await sqs_client.delete_message(
                QueueUrl=queue_url,
                ReceiptHandle=message["ReceiptHandle"],
            )

            _LOGGER.debug("Successfully processed and deleted message")

        except json.JSONDecodeError as err:
            _LOGGER.error("Failed to parse message JSON: %s", err)
            _LOGGER.debug("Message body: %s", message.get("Body"))
        except Exception as err:
            _LOGGER.exception("Error processing message: %s", err)


class MockSQSClient:
    """Mock SQS client for testing without AWS credentials.

    This allows the integration to work without SQS (polling fallback).
    """

    def __init__(self, *args, **kwargs):
        """Initialize mock client."""
        _LOGGER.warning(
            "Using mock SQS client. Real-time events will not work. "
            "Configure AWS SQS credentials for instant event delivery."
        )

    def set_event_callback(self, callback):
        """Set event callback (no-op)."""
        pass

    async def start(self):
        """Start (no-op)."""
        _LOGGER.debug("Mock SQS client started (no-op)")

    async def stop(self):
        """Stop (no-op)."""
        _LOGGER.debug("Mock SQS client stopped (no-op)")
