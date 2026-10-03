import time
import logging

from redis import Redis
from circuitbreaker import circuit
from redis.exceptions import (
    RedisError,
    ConnectionError as RedisConnectionError,
    ResponseError,
)

from genesis.config import REDIS_HOST, REDIS_PORT

logger = logging.getLogger(__name__)


# ============================================================================
# 🔹 REDIS CONNECTION WITH RETRY LOGIC
# ============================================================================
@circuit(failure_threshold=3, expected_exception=RedisError, recovery_timeout=60)
def create_redis_connection(
    host: str = REDIS_HOST,
    port: int = REDIS_PORT,
    db: int = 0,
    max_retries: int = 3,
    **kwargs,
) -> Redis:
    for attempt in range(max_retries):
        try:
            client = Redis(
                host=host,
                port=port,
                db=db,
                decode_responses=True,
                socket_timeout=10,
                socket_connect_timeout=5,
                retry_on_timeout=True,
                health_check_interval=30,
                max_connections=50,
                **kwargs,
            )
            client.ping()
            logger.info(f"✅ Redis connection established (DB={db}) at {host}:{port}")
            return client

        except (RedisConnectionError, RedisError) as e:
            logger.warning(f"⚠️ Redis connection attempt {attempt + 1} failed: {e}")
            if attempt < max_retries - 1:
                time.sleep(2**attempt)

    # 🔒 Guarantees the function never implicitly returns None
    raise RedisConnectionError(
        f"❌ Failed to connect to Redis at {host}:{port} (DB={db}) after {max_retries} attempts"
    )


# ============================================================================
# 🔹 SECURITY & HELPER UTILITIES OF REDIS
# ============================================================================
@circuit(failure_threshold=5, expected_exception=RedisError, recovery_timeout=30)
def safe_redis_operation(operation, *args, **kwargs):
    """
    Execute Redis operations with error handling.

    Args:
        operation: Redis operation function
        *args, **kwargs: Arguments for the operation

    Returns:
        Result of operation or None on failure
    """
    try:
        return operation(*args, **kwargs)
    except RedisError as e:
        logger.error(f"❌ Redis operation failed: {e}", exc_info=True)
        return None


class RedisStreamHandler:
    """
    🔹 RedisStreamHandler

    RESPONSIBILITIES:
    - Ensure the stream exists.
    - Ensure a consumer group exists (idempotent).
    - Publish messages with optional length- or time-based trimming.
    - Provide helpers to inspect groups and consumers.
    """

    def __init__(
        self, redis_broker, stream_key, group_name, maxlen=None, max_age_seconds=None
    ):
        self.redis = redis_broker
        self.stream_key = stream_key
        self.group_name = group_name
        self.maxlen = maxlen
        self.max_age_seconds = max_age_seconds
        self.safe_op = safe_redis_operation

        # Cache to avoid repeated xinfo_groups calls in loops
        self._stream_initialized = False

        # Ensure the stream and consumer group exist at initialization
        self._ensure_stream_and_group()

    def _ensure_stream_and_group(self):
        """
        Efficiently ensure the stream and consumer group exist.

        1. Creates the stream if it doesn't exist.
        2. Creates the group if it doesn't exist.
        3. Idempotent and safe for repeated calls.
        """
        if self._stream_initialized:
            return  # Already initialized in this process

        try:
            # Check if stream exists
            if not self.redis.exists(self.stream_key):
                # Stream doesn't exist, add a dummy entry
                self.redis.xadd(self.stream_key, {"init": "true"})
                logger.info("✅ Redis stream '%s' created", self.stream_key)

            # Check existing groups
            try:
                groups = self.redis.xinfo_groups(self.stream_key)
                existing_groups = [g["name"] for g in groups]
            except ResponseError:
                # Stream exists but has no groups
                existing_groups = []

            if self.group_name not in existing_groups:
                self.redis.xgroup_create(
                    self.stream_key,
                    self.group_name,
                    id="0-0",
                )
                logger.info(
                    "✅ Redis consumer group '%s' created on stream '%s'",
                    self.group_name,
                    self.stream_key,
                )
            else:
                logger.debug(
                    "✅ Redis consumer group '%s' already exists on stream '%s'",
                    self.group_name,
                    self.stream_key,
                )

            # Mark as initialized to skip future redundant calls
            self._stream_initialized = True

        except RedisError as e:
            logger.error(
                "❌ Failed to ensure stream '%s' and group '%s'",
                self.stream_key,
                self.group_name,
                exc_info=True,
            )
            raise

    # -------------------- Publish/Consume/Trim Methods --------------------

    def publish(self, payload):
        """Publish a message with optional trimming."""
        if self.max_age_seconds:
            self.safe_op(self.trim_by_age, self.max_age_seconds)

        self.safe_op(
            self.redis.xadd,
            self.stream_key,
            {"payload": payload},
            maxlen=self.maxlen,
            approximate=True,
        )

    def consume(
        self,
        last_id: str = "0-0",
        count: int = 10,
        block_ms: int = 0,
        use_group: bool = False,
        consumer_name: str | None = None,
    ):
        if use_group and not consumer_name:
            raise ValueError("consumer_name must be provided when using use_group=True")

        if use_group:
            stream_arg = {self.stream_key: ">"}
            result = self.safe_op(
                self.redis.xreadgroup,
                groupname=self.group_name,
                consumername=consumer_name,
                streams=stream_arg,
                count=count,
                block=block_ms or None,
            )
            if not result:
                return []

            _, messages = result[0]
        else:
            result = self.safe_op(
                self.redis.xrange,
                self.stream_key,
                min=last_id,
                max="+",
                count=count,
            )
            if not result:
                return []
            messages = result

        return [(msg_id, data.get("payload")) for msg_id, data in messages]

    def _acknowledge(self, message_id: str):
        """Acknowledge processed message."""
        self.safe_op(
            self.redis.xack,
            self.stream_key,
            self.group_name,
            message_id,
        )

    def trim_by_age(self, max_age_seconds: int):
        """Trim messages older than max_age_seconds."""
        now_ms = int(time.time() * 1000)
        cutoff_ms = now_ms - (max_age_seconds * 1000)
        cutoff_id = f"{cutoff_ms}-0"

        expired = self.safe_op(
            self.redis.xrange,
            self.stream_key,
            min="-",
            max=cutoff_id,
        )

        if not expired:
            return

        expired_ids = [msg_id for msg_id, _ in expired]
        self.safe_op(self.redis.xdel, self.stream_key, *expired_ids)

    def list_consumers(self):
        return self.safe_op(
            self.redis.xinfo_consumers, self.stream_key, self.group_name
        )

    def list_groups(self):
        return self.safe_op(self.redis.xinfo_groups, self.stream_key)

    def backlog_count(self) -> int:
        """Messages waiting for or assigned to this consumer group."""
        groups = self.safe_op(self.redis.xinfo_groups, self.stream_key)
        if not groups:
            return 0
        for group in groups:
            if group.get("name") == self.group_name:
                pending = int(group.get("pending", 0) or 0)
                lag = int(group.get("lag", 0) or 0)
                return pending + lag
        return 0


# ============================================================================
# 🔹 REDIS CONNECTION INSTANCES
# ============================================================================
# One database per concern — separate namespaces, separate lifetimes:
#   DB 1 — verdicts:     ad ids with a FINAL verdict (accepted or
#                        judged-rejected). Permanent by design; the
#                        never-process-twice memory.
#   DB 2 — run state:    progress of long-running scrapes (resume support).
#   DB 3 — keywords:     keyword cooldowns (TTL — keys expire themselves)
#                        + persisted exclusion prompts.

verdict_broker = create_redis_connection(db=1)
run_state_broker = create_redis_connection(db=2)
keyword_broker = create_redis_connection(db=3)
