import threading
import uuid

import pytest

from prefect.filesystems import LocalFileSystem
from prefect.flows import flow
from prefect.records import RecordStore
from prefect.records.memory import MemoryRecordStore
from prefect.records.result_store import ResultFactoryStore
from prefect.results import (
    ResultFactory,
    get_default_result_storage,
    get_or_create_default_task_scheduling_storage,
)
from prefect.settings import (
    PREFECT_DEFAULT_RESULT_STORAGE_BLOCK,
    PREFECT_TASK_SCHEDULING_DEFAULT_STORAGE_BLOCK,
    temporary_settings,
)
from prefect.tasks import task
from prefect.transactions import (
    CommitMode,
    IsolationLevel,
    Transaction,
    TransactionState,
    get_transaction,
    transaction,
)
from prefect.utilities.asyncutils import run_coro_as_sync


def test_basic_init():
    txn = Transaction()
    assert txn.store is None
    assert txn.state == TransactionState.PENDING
    assert txn.is_pending()
    assert txn.commit_mode is None


def test_equality():
    txn1 = Transaction()
    txn2 = Transaction()
    txn3 = Transaction(key="test")
    assert txn1 == txn2
    assert txn1 != txn3


class TestGetTxn:
    def test_get_transaction(self):
        assert get_transaction() is None
        with Transaction() as txn:
            assert get_transaction() == txn
        assert get_transaction() is None

    def test_nested_get_transaction(self):
        assert get_transaction() is None
        with Transaction(key="outer") as outer:
            assert get_transaction() == outer
            with Transaction(key="inner") as inner:
                assert get_transaction() == inner
            assert get_transaction() == outer
        assert get_transaction() is None

    def test_txn_resets_even_with_error(self):
        assert get_transaction() is None

        with pytest.raises(ValueError, match="foo"):
            with Transaction() as txn:
                assert get_transaction() == txn
                raise ValueError("foo")

        assert get_transaction() is None


class TestGetParent:
    def test_get_parent(self):
        with Transaction() as txn:
            assert txn.get_parent() is None

    def test_get_parent_with_parent(self):
        with Transaction(key="outer") as outer:
            assert outer.get_parent() is None
            with Transaction(key="inner") as inner:
                assert inner.get_parent() == outer
            assert outer.get_parent() is None


class TestCommitMode:
    def test_txns_dont_auto_commit(self):
        with Transaction(key="outer") as outer:
            assert not outer.is_committed()

            with Transaction(key="inner") as inner:
                pass

            assert not inner.is_committed()

        assert outer.is_committed()
        assert inner.is_committed()

    def test_txns_auto_commit_in_eager(self):
        with Transaction(commit_mode=CommitMode.EAGER) as txn:
            assert txn.is_active()
            assert not txn.is_committed()
        assert txn.is_committed()

        with Transaction(key="outer", commit_mode=CommitMode.EAGER) as outer:
            assert not outer.is_committed()
            with Transaction(key="inner") as inner:
                pass
            assert inner.is_committed()
        assert outer.is_committed()

    def test_txns_dont_commit_on_exception(self):
        with pytest.raises(ValueError, match="foo"):
            with Transaction() as txn:
                raise ValueError("foo")

        assert not txn.is_committed()
        assert txn.is_rolled_back()

    def test_txns_dont_commit_on_rollback(self):
        with Transaction() as txn:
            txn.rollback()

        assert not txn.is_committed()
        assert txn.is_rolled_back()

    def test_txns_commit_with_lazy_parent_if_eager(self):
        with Transaction(key="outer", commit_mode=CommitMode.LAZY) as outer:
            assert not outer.is_committed()

            with Transaction(key="inner", commit_mode=CommitMode.EAGER) as inner:
                pass

            assert inner.is_committed()

        assert outer.is_committed()

    def test_txns_commit_off_rolls_back_on_exit(self):
        with Transaction(commit_mode=CommitMode.OFF) as txn:
            assert not txn.is_committed()
        assert not txn.is_committed()
        assert txn.is_rolled_back()

    def test_txns_commit_off_doesnt_roll_back_if_committed(self):
        with Transaction(commit_mode=CommitMode.OFF) as txn:
            txn.commit()
        assert txn.is_committed()
        assert not txn.is_rolled_back()

    def test_error_in_commit_triggers_rollback(self):
        class BadTxn(Transaction):
            def commit(self, **kwargs):
                raise ValueError("foo")

        with Transaction() as txn:
            txn.add_child(BadTxn())

        assert not txn.is_committed()
        assert txn.is_rolled_back()


class TestRollBacks:
    def test_rollback_flag_is_set(self):
        with Transaction() as txn:
            assert not txn.is_rolled_back()
            txn.rollback()
            assert txn.is_rolled_back()

    def test_txns_rollback_on_exception(self):
        with pytest.raises(ValueError, match="foo"):
            with Transaction() as txn:
                raise ValueError("foo")

        assert txn.is_rolled_back()

    def test_rollback_flag_propagates_up(self):
        with Transaction(key="outer") as outer:
            assert not outer.is_rolled_back()
            with Transaction(key="inner") as inner:
                assert not inner.is_rolled_back()
                with Transaction(key="nested") as nested:
                    assert not nested.is_rolled_back()
                    nested.rollback()
                    assert nested.is_rolled_back()
                assert inner.is_rolled_back()
        assert outer.is_rolled_back()

    def test_failed_rollback_resets_txn(self):
        class BadTxn(Transaction):
            def rollback(self, **kwargs):
                raise ValueError("foo")

        assert get_transaction() is None

        with Transaction(key="outer") as txn:
            txn.add_child(BadTxn())
            assert txn.rollback() is False  # rollback failed

        assert get_transaction() is None


class TestTransactionState:
    @pytest.mark.parametrize("state", TransactionState.__members__.values())
    def test_state_and_methods_are_consistent(self, state):
        "Not the best test, but it does the job"
        txn = Transaction(state=state)
        assert txn.is_active() == (txn.state.name == "ACTIVE")
        assert txn.is_pending() == (txn.state.name == "PENDING")
        assert txn.is_committed() == (txn.state.name == "COMMITTED")
        assert txn.is_staged() == (txn.state.name == "STAGED")
        assert txn.is_rolled_back() == (txn.state.name == "ROLLED_BACK")

    def test_happy_state_lifecycle(self):
        txn = Transaction()
        assert txn.is_pending()

        with txn:
            assert txn.is_active()

        assert txn.is_committed()

    def test_unhappy_state_lifecycle(self):
        txn = Transaction()
        assert txn.is_pending()

        with pytest.raises(ValueError, match="foo"):
            with txn:
                assert txn.is_active()
                raise ValueError("foo")

        assert txn.is_rolled_back()


def test_overwrite_ignores_existing_record():
    class Store(RecordStore):
        def exists(self, **kwargs):
            return True

        def read(self, **kwargs):
            return "done"

        def write(self, **kwargs):
            pass

        def supports_isolation_level(self, *args, **kwargs):
            return True

    with Transaction(
        key="test_overwrite_ignores_existing_record", store=Store()
    ) as txn:
        assert txn.is_committed()

    with Transaction(
        key="test_overwrite_ignores_existing_record", store=Store(), overwrite=True
    ) as txn:
        assert not txn.is_committed()


class TestDefaultTransactionStorage:
    @pytest.fixture(autouse=True)
    def default_storage_setting(self, tmp_path):
        name = str(uuid.uuid4())
        LocalFileSystem(basepath=tmp_path).save(name)
        with temporary_settings(
            {
                PREFECT_DEFAULT_RESULT_STORAGE_BLOCK: f"local-file-system/{name}",
                PREFECT_TASK_SCHEDULING_DEFAULT_STORAGE_BLOCK: f"local-file-system/{name}",
            }
        ):
            yield

    async def test_transaction_outside_of_run(self):
        with transaction(key="test_transaction_outside_of_run") as txn:
            assert isinstance(txn.store, ResultFactoryStore)
            result = await txn.store.result_factory.create_result(
                obj={"foo": "bar"}, key=txn.key
            )
            txn.stage(result)

        result = txn.read()
        assert result
        assert await result.get() == {"foo": "bar"}

    async def test_transaction_inside_flow_default_storage(self):
        @flow
        def test_flow():
            with transaction(key="test_transaction_inside_flow_default_storage") as txn:
                assert isinstance(txn.store, ResultFactoryStore)
                result = txn.store.result_factory.create_result(
                    obj={"foo": "bar"}, key=txn.key, _sync=True
                )
                txn.stage(result)

            result = txn.read()
            assert result
            # make sure we aren't using an anonymous block
            assert (
                result.storage_block_id
                == get_default_result_storage()._block_document_id
            )
            return result.get()

        assert test_flow() == {"foo": "bar"}

    async def test_transaction_inside_flow_configured_storage(self, tmp_path):
        block = LocalFileSystem(basepath=tmp_path)
        await block.save("test-transaction-inside-flow-configured-storage")

        @flow(result_storage=block)
        async def test_flow():
            with transaction(
                key="test_transaction_inside_flow_configured_storage"
            ) as txn:
                assert isinstance(txn.store, ResultFactoryStore)
                result = await txn.store.result_factory.create_result(
                    obj={"foo": "bar"}, key=txn.key
                )
                txn.stage(result)

            result = txn.read()
            assert result
            assert result.storage_block_id == block._block_document_id
            return await result.get()

        assert await test_flow() == {"foo": "bar"}

    async def test_transaction_inside_task_default_storage(self):
        default_task_storage = await get_or_create_default_task_scheduling_storage()

        @task
        async def test_task():
            with transaction(key="test_transaction_inside_task_default_storage") as txn:
                assert isinstance(txn.store, ResultFactoryStore)
                result = await txn.store.result_factory.create_result(
                    obj={"foo": "bar"}, key=txn.key
                )
                txn.stage(result)

            result = txn.read()
            assert result
            # make sure we aren't using an anonymous block
            assert result.storage_block_id == default_task_storage._block_document_id
            return await result.get()

        assert await test_task() == {"foo": "bar"}

    async def test_transaction_inside_task_configured_storage(self, tmp_path):
        block = LocalFileSystem(basepath=tmp_path)
        await block.save("test-transaction-inside-task-configured-storage")

        @task(result_storage=block)
        async def test_task():
            with transaction(
                key="test_transaction_inside_task_configured_storage"
            ) as txn:
                assert isinstance(txn.store, ResultFactoryStore)
                result = await txn.store.result_factory.create_result(
                    obj={"foo": "bar"}, key=txn.key
                )
                txn.stage(result)

            result = txn.read()
            assert result
            assert result.storage_block_id == block._block_document_id
            return await result.get()

        assert await test_task() == {"foo": "bar"}


class TestWithMemoryRecordStore:
    @pytest.fixture()
    def default_storage_setting(self, tmp_path):
        name = str(uuid.uuid4())
        LocalFileSystem(basepath=tmp_path).save(name)
        with temporary_settings(
            {
                PREFECT_DEFAULT_RESULT_STORAGE_BLOCK: f"local-file-system/{name}",
                PREFECT_TASK_SCHEDULING_DEFAULT_STORAGE_BLOCK: f"local-file-system/{name}",
            }
        ):
            yield

    @pytest.fixture
    async def result_1(self, default_storage_setting):
        result_factory = await ResultFactory.default_factory(persist_result=True)
        return await result_factory.create_result(obj={"foo": "bar"})

    @pytest.fixture
    async def result_2(self, default_storage_setting):
        result_factory = await ResultFactory.default_factory(persist_result=True)
        return await result_factory.create_result(obj={"fizz": "buzz"})

    async def test_basic_transaction(self, result_1):
        store = MemoryRecordStore()
        with transaction(key="test_basic_transaction", store=store) as txn:
            assert isinstance(txn.store, MemoryRecordStore)
            txn.stage(result_1)

        result_1 = txn.read()
        assert result_1
        assert await result_1.get() == {"foo": "bar"}

        record = store.read("test_basic_transaction")
        assert record
        assert record.result == result_1
        assert record.key == "test_basic_transaction"

    async def test_competing_read_transaction(self, result_1):
        transaction_1_open = threading.Event()
        transaction_2_open = threading.Event()
        store = MemoryRecordStore()

        def writing_transaction():
            # isolation level is SERIALIZABLE, so a lock will be taken
            with transaction(
                key="test_competing_read_transaction",
                store=store,
                isolation_level=IsolationLevel.SERIALIZABLE,
            ) as txn:
                transaction_1_open.set()
                transaction_2_open.wait()
                txn.stage(result_1)

        thread = threading.Thread(target=writing_transaction)
        thread.start()
        transaction_1_open.wait()
        with transaction(key="test_competing_read_transaction", store=store) as txn:
            transaction_2_open.set()
            read_result = txn.read()

        assert read_result == result_1
        thread.join()

    async def test_competing_write_transaction(self, result_1, result_2):
        transaction_1_open = threading.Event()
        store = MemoryRecordStore()

        def winning_transaction():
            with transaction(
                key="test_competing_write_transaction",
                store=store,
                isolation_level=IsolationLevel.SERIALIZABLE,
            ) as txn:
                transaction_1_open.set()
                txn.stage(result_1)

        thread = threading.Thread(target=winning_transaction)
        thread.start()
        transaction_1_open.wait()
        with transaction(
            key="test_competing_write_transaction",
            store=store,
            isolation_level=IsolationLevel.SERIALIZABLE,
        ) as txn:
            txn.stage(result_2)

        thread.join()
        record = store.read("test_competing_write_transaction")
        assert record
        # the first transaction should have written its result
        # and the second transaction should not have written on exit
        assert record.result == result_1


class TestHooks:
    def test_get_and_set_data(self):
        with transaction(key="test") as txn:
            txn.set("x", 42)
            assert txn.get("x") == 42

    def test_get_and_set_data_in_nested_context(self):
        with transaction(key="test") as top:
            top.set("key", 42)
            with transaction(key="nested") as inner:
                assert (
                    inner.get("key") == 42
                )  # children inherit from their parents first
                inner.set("key", "string")  # and can override
                assert inner.get("key") == "string"
                assert top.get("key") == 42
            assert top.get("key") == 42

    def test_get_and_set_data_doesnt_mutate_parent(self):
        with transaction(key="test") as top:
            top.set("key", {"x": [42]})
            with transaction(key="nested") as inner:
                inner.get("key")["x"].append(43)
                assert inner.get("key") == {"x": [42, 43]}
                assert top.get("key") == {"x": [42]}
            assert top.get("key") == {"x": [42]}

    def test_get_raises_on_unknown_but_allows_default(self):
        with transaction(key="test") as txn:
            with pytest.raises(ValueError, match="foobar"):
                txn.get("foobar")
            assert txn.get("foobar", None) is None
            assert txn.get("foobar", "string") == "string"


class TestIsolationLevel:
    def test_default_isolation_level(self):
        with transaction(key="test") as txn:
            assert txn.isolation_level == IsolationLevel.READ_COMMITTED

    def test_inherited_isolation_level(self):
        with transaction(
            key="test",
            store=MemoryRecordStore(),
            isolation_level=IsolationLevel.SERIALIZABLE,
        ) as top:
            with transaction(key="nested", store=MemoryRecordStore()) as inner:
                assert (
                    inner.isolation_level
                    == top.isolation_level
                    == IsolationLevel.SERIALIZABLE
                )

    def test_raises_on_unsupported_isolation_level(self):
        with pytest.raises(
            ValueError,
            match="Isolation level SERIALIZABLE is not supported by record store type ResultFactoryStore",
        ):
            with transaction(
                key="test",
                store=ResultFactoryStore(
                    result_factory=run_coro_as_sync(ResultFactory.default_factory())
                ),
                isolation_level=IsolationLevel.SERIALIZABLE,
            ):
                pass
