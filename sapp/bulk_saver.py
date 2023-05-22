# Copyright (c) Meta Platforms, Inc. and affiliates.
#
# This source code is licensed under the MIT license found in the
# LICENSE file in the root directory of this source tree.

"""Bulk saving objects for performance
"""

import logging
from typing import Any, Callable, Dict, Optional

from sqlalchemy.exc import IntegrityError

from .db import DB
from .decorators import log_time
from .iterutil import split_every
from .models import (
    ClassTypeInterval,
    Issue,
    IssueInstance,
    IssueInstanceFixInfo,
    IssueInstanceSharedTextAssoc,
    IssueInstanceTraceFrameAssoc,
    MetaRunIssueInstanceIndex,
    PrimaryKeyGenerator,
    SharedText,
    TraceFrame,
    TraceFrameAnnotation,
    TraceFrameAnnotationTraceFrameAssoc,
    TraceFrameLeafAssoc,
)

log: logging.Logger = logging.getLogger("sapp")


class BulkSaver:
    """Stores new objects created within a run and bulk save them"""

    # order is significant, objects will be saved in this order.
    SAVING_CLASSES_ORDER = [
        SharedText,
        Issue,
        IssueInstanceFixInfo,
        IssueInstance,
        IssueInstanceSharedTextAssoc,
        TraceFrame,
        IssueInstanceTraceFrameAssoc,
        TraceFrameAnnotation,
        TraceFrameLeafAssoc,
        TraceFrameAnnotationTraceFrameAssoc,
        ClassTypeInterval,
        MetaRunIssueInstanceIndex,
    ]

    BATCH_SIZE = 30000

    # The number of sub-batches to split the parent batch into before retrying
    # on duplicate key exceptions.
    #
    # Assuming there are only a couple of duplicate records per batch,
    # splitting in ~4 seems to give reasonable behavior.
    #
    # Lower factors (like 2) lead to a bit more unnecessary traffic as we re-send
    # more large batches while taking longer to isolate the duplicate record(s).
    #
    # Higher factors lead to too many round-trips with the database.
    # At the extreme, we would send a request for every individual row in the batch.
    BATCH_SPLIT_FACTOR = 4

    def __init__(
        self, primary_key_generator: Optional[PrimaryKeyGenerator] = None
    ) -> None:
        self.primary_key_generator: PrimaryKeyGenerator = (
            primary_key_generator or PrimaryKeyGenerator()
        )
        self.saving: Dict[str, Any] = {}
        for cls in self.SAVING_CLASSES_ORDER:
            self.saving[cls.__name__] = []

    # pyre-fixme[2]: Parameter must be annotated.
    def add(self, item) -> None:
        assert item.model in self.SAVING_CLASSES_ORDER, (
            "%s should be added with session.add()" % item.model.__name__
        )
        self.saving[item.model.__name__].append(item)

    # pyre-fixme[2]: Parameter must be annotated.
    def add_all(self, items) -> None:
        if items:
            assert items[0].model in self.SAVING_CLASSES_ORDER, (
                "%s should be added with session.add_all()" % items[0].model.__name__
            )
            self.saving[items[0].model.__name__].extend(items)

    # pyre-fixme[3]: Return type must be annotated.
    # pyre-fixme[2]: Parameter must be annotated.
    def get_items_to_add(self, cls):
        return self.saving[cls.__name__]

    def save_all(
        self, database: DB, before_save: Optional[Callable[[], None]] = None
    ) -> None:
        saving_classes = [
            cls
            for cls in self.SAVING_CLASSES_ORDER
            if len(self.saving[cls.__name__]) != 0
        ]

        item_counts = {
            cls.__name__: len(self.get_items_to_add(cls)) for cls in saving_classes
        }

        with database.make_session() as session:
            pk_gen = self.primary_key_generator.reserve(
                session, saving_classes, item_counts
            )

        for cls in saving_classes:
            log.info("Merging and generating ids for %s...", cls.__name__)
            self._prepare(database, cls, pk_gen)

        # Used by unit tests to simulate races
        if before_save:
            before_save()

        for cls in saving_classes:
            log.info("Saving %s...", cls.__name__)
            self._save(database, cls, pk_gen)

    @log_time
    # pyre-fixme[2]: Parameter must be annotated.
    def _prepare(self, database: DB, cls, pk_gen: PrimaryKeyGenerator) -> None:
        # We sort keys because bulk insert uses executemany, but it can only
        # group together sequential items with the same keys. If we are scattered
        # then it does far more executemany calls, and it kills performance.
        items = sorted(
            cls.prepare(database, pk_gen, self.saving[cls.__name__]),
            key=lambda r: list(cls.to_dict(r).keys()),
        )
        self.saving[cls.__name__] = items

    @log_time
    # pyre-fixme[2]: Parameter must be annotated.
    def _save(self, database: DB, cls, pk_gen: PrimaryKeyGenerator) -> None:
        items = self.saving[cls.__name__]
        self.saving[cls.__name__] = []  # allow GC after we are done

        # bulk_insert_mappings should only be used for new objects.
        # To update an existing object, just modify its attribute(s)
        # and call session.commit()
        for batch in split_every(self.BATCH_SIZE, items):
            round_trips = self._save_batch(database, cls, batch)
            if round_trips > 1:
                log.info(
                    f"Saving {cls.__name__} batch of {len(batch)} "
                    f"took {round_trips} round trips due to duplicate key retries"
                )

    # Save a batch of records to the database, handling duplicate key errors
    # by retrying in smaller batches until all non-duplicate records have been
    # inserted and all duplicates have been merged with existing rows.
    #
    # Why is this needed when we already merge duplicates in `_prepare`?
    # There is a race where another script can insert a duplicate after `_prepare` but
    # before `_save`.
    #
    # pyre-fixme[2]: Parameter must be annotated.
    def _save_batch(self, database: DB, cls, batch) -> int:
        round_trips = 1
        try:
            with database.make_session() as session:
                session.bulk_insert_mappings(
                    cls, (cls.to_dict(r) for r in batch), render_nulls=True
                )
                session.commit()
            return round_trips
        # "Duplicate entry for key" errors are surfaced as IntegrityError
        except IntegrityError as e:
            if len(batch) == 1:
                # As the batch only contains one record, we know that this was
                # the cause of the failure.
                #
                # Call the merge implementation again. It should now resolve
                # the duplicate's id to the id of the existing row.
                duplicate = batch[0]
                if len(list(cls.merge(database, [duplicate]))) == 0:
                    log.debug(f"Re-merged duplicate record during saving: {duplicate}")
                else:
                    raise ValueError(
                        f"Got a duplicate key exception that was not resolved "
                        f"by {cls.__name__}.merge: {duplicate}"
                    ) from e
            else:
                # The batch contained multiple items, so we don't which record
                # caused the failure. Split into smaller "sub_batches" and retry.
                #
                # Negations are ceiling integer division to avoid batch size of 0
                sub_batch_size = -(len(batch) // -self.BATCH_SPLIT_FACTOR)
                for sub_batch in split_every(sub_batch_size, batch):
                    round_trips += self._save_batch(database, cls, sub_batch)
            return round_trips

    def add_trace_frame_leaf_assoc(
        self, message: SharedText, trace_frame: TraceFrame, depth: Optional[int]
    ) -> None:
        self.add(
            TraceFrameLeafAssoc.Record(
                trace_frame_id=trace_frame.id, leaf_id=message.id, trace_length=depth
            )
        )

    def add_issue_instance_trace_frame_assoc(
        self, issue_instance: IssueInstance, trace_frame: TraceFrame
    ) -> None:
        self.add(
            IssueInstanceTraceFrameAssoc.Record(
                issue_instance_id=issue_instance.id, trace_frame_id=trace_frame.id
            )
        )

    def add_issue_instance_shared_text_assoc(
        self, issue_instance: IssueInstance, shared_text: SharedText
    ) -> None:
        self.add(
            IssueInstanceSharedTextAssoc.Record(
                issue_instance_id=issue_instance.id, shared_text_id=shared_text.id
            )
        )

    def add_trace_frame_annotation_trace_frame_assoc(
        self,
        trace_frame_annotation: TraceFrameAnnotation,
        trace_frame: TraceFrame,
    ) -> None:
        self.add(
            TraceFrameAnnotationTraceFrameAssoc.Record(
                trace_frame_annotation_id=trace_frame_annotation.id,
                trace_frame_id=trace_frame.id,
            )
        )

    def dump_stats(self) -> str:
        stat_str = ""
        for cls in self.SAVING_CLASSES_ORDER:
            stat_str += "%s: %d\n" % (cls.__name__, len(self.saving[cls.__name__]))
        return stat_str
