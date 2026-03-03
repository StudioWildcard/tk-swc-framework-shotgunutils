# Copyright (c) 2016 Shotgun Software Inc.
#
# CONFIDENTIAL AND PROPRIETARY
#
# This work is provided "AS IS" and subject to the Shotgun Pipeline Toolkit
# Source Code License included in this distribution package. See LICENSE.
# By accessing, using, copying or modifying this work you indicate your
# agreement to the Shotgun Pipeline Toolkit Source Code License. All rights
# not expressly granted therein are reserved by Shotgun Software Inc.

import sgtk
import copy
import os
import sys
import hashlib
from collections import defaultdict

from sgtk.platform.qt import QtCore, QtGui

from .shotgun_standard_item import ShotgunStandardItem
from .shotgun_query_model import ShotgunQueryModel
from .data_handler_find import ShotgunFindDataHandler
from .util import get_sanitized_data, get_sg_data, sanitize_for_qt_model

class ShotgunModel(ShotgunQueryModel):
    """
    A Qt Model representing a Shotgun query.
    """

    SG_ASSOCIATED_FIELD_ROLE = QtCore.Qt.UserRole + 10
    FIRST_COLUMN_HEADER = "Name"

    def __init__(
        self,
        parent,
        download_thumbs=True,
        schema_generation=0,
        bg_load_thumbs=True,
        bg_task_manager=None,
    ):
        super(ShotgunModel, self).__init__(parent, bg_load_thumbs, bg_task_manager)
        self.__entity_type = None
        self.__schema_generation = schema_generation
        self.__download_thumbs = download_thumbs

    def __repr__(self):
        return "<%s entity_type:%s>" % (self.__class__.__name__, self.__entity_type)

    @property
    def entity_ids(self):
        return self._data_handler.get_entity_ids() if self._data_handler else []

    def item_from_entity(self, entity_type, entity_id):
        self._log_debug("Resolving model item for entity %s:%s" % (entity_type, entity_id))
        if entity_type != self.__entity_type:
            self._log_debug("...entity type is not part of this model!")
            return None
        uid = self._data_handler.get_uid_from_entity_id(entity_id)
        if uid is None:
            self._log_debug("...entity id is not part of the data set")
            return None
        return self._ensure_item_loaded(uid)

    def index_from_entity(self, entity_type, entity_id):
        item = self.item_from_entity(entity_type, entity_id)
        return self.indexFromItem(item) if item else None

    def get_filters(self, item):
        filters = copy.deepcopy(self.__filters)
        p = item
        while p:
            field_data = get_sanitized_data(p, self.SG_ASSOCIATED_FIELD_ROLE)
            filters.append([field_data["name"], "is", field_data["value"]])
            p = p.parent()
        return filters

    def ensure_data_is_loaded(self, index=None):
        if index is None:
            index = self.invisibleRootItem().index()
        if self.canFetchMore(index):
            self.fetchMore(index)
        for child_index in range(self.rowCount(index)):
            child_model_index = self.index(child_index, 0, parent=index)
            self.ensure_data_is_loaded(child_model_index)

    def get_entity_type(self):
        return self.__entity_type

    def get_additional_column_fields(self):
        return [
            {"column_idx": i + 1, "field": field}
            for (i, field) in enumerate(self.__column_fields)
        ]

    def _load_data(
        self,
        entity_type,
        filters,
        hierarchy,
        fields,
        order=None,
        seed=None,
        limit=None,
        columns=None,
        additional_filter_presets=None,
        editable_columns=None,
        sg_data_type=None
    ):
        """
        Configures the model with a specific Shotgun query.
        """
        self.query_changed.emit()
        self.clear()

        self.__entity_type = entity_type
        self.__filters = filters
        self.__fields = fields
        self.__order = order or []
        self.__hierarchy = hierarchy
        self.__column_fields = columns or []
        self.__editable_fields = editable_columns or []
        self.__limit = limit or 0
        self.__additional_filter_presets = additional_filter_presets
        self.__sg_data_type = sg_data_type

        if not set(self.__editable_fields).issubset(set(self.__column_fields)):
            raise sgtk.TankError("`editable_fields` is not a subset of `column_fields`.")

        self._log_debug("")
        self._log_debug("Model Reset for %s" % self)
        self._log_debug("Entity type: %s" % self.__entity_type)
        self._log_debug("sg data type: %s" % self.__sg_data_type)
        self._log_debug("Filters: %s" % self.__filters)
        self._log_debug("Hierarchy: %s" % self.__hierarchy)
        self._log_debug("Fields: %s" % self.__fields)
        self._log_debug("Order: %s" % self.__order)
        self._log_debug("Columns: %s" % self.__column_fields)
        self._log_debug("Editable Columns: %s" % self.__editable_fields)
        self._log_debug("Filter Presets: %s" % self.__additional_filter_presets)

        self._bundle = sgtk.platform.current_bundle()
        self._data_handler = ShotgunFindDataHandler(
            self.__entity_type,
            self.__filters,
            self.__order,
            self.__hierarchy,
            self.__fields + self.__column_fields,
            self.__download_thumbs,
            self.__limit,
            self.__additional_filter_presets,
            self.__compute_cache_path(seed),
            sg_data_type=self.__sg_data_type
        )
        self._log_debug("Loading data from cache file into memory...")
        self._data_handler.load_cache()
        self._log_debug("First population pass: Calling _load_external_data()")
        self._load_external_data()
        self._log_debug("External data population done.")

        headers = [self.FIRST_COLUMN_HEADER] + self._get_additional_column_headers(
            self.__entity_type, self.__column_fields
        )
        self.setHorizontalHeaderLabels(headers)

        root = self.invisibleRootItem()
        self._log_debug("Creating model nodes for top level of data tree...")
        nodes_generated = self._data_handler.generate_child_nodes(
            None, root, self._create_item
        )

        if nodes_generated > 0:
            self.cache_loaded.emit()

        return nodes_generated > 0

    def _refresh_data(self):
        self._request_data()

    def _item_created(self, item):
        super(ShotgunModel, self)._item_created(item)
        if self.__download_thumbs:
            sg_data = item.data(self.SG_DATA_ROLE)
            if sg_data:
                for field in sg_data.keys():
                    if "image" in field and sg_data[field] is not None:
                        self._request_thumbnail_download(
                            item, field, sg_data[field], sg_data.get("type"), sg_data.get("id")
                        )

    def _set_tooltip(self, item, sg_item):
        data = item.data(self.SG_ASSOCIATED_FIELD_ROLE)
        field = data["name"]
        if isinstance(sg_item[field], dict) and "type" in sg_item[field]:
            item.setToolTip(
                "%s '%s'" % (
                    self._shotgun_globals.get_type_display_name(sg_item[field]["type"]),
                    item.text(),
                )
            )
        elif "." in field:
            _, sub_entity_type, sub_entity_field_name = field.rsplit(".", 2)
            item.setToolTip(
                "%s %s '%s'" % (
                    self._shotgun_globals.get_type_display_name(sub_entity_type),
                    self._shotgun_globals.get_field_display_name(sub_entity_type, sub_entity_field_name),
                    item.text(),
                )
            )
        else:
            item.setToolTip(
                "%s '%s'" % (
                    self._shotgun_globals.get_type_display_name(sg_item["type"]),
                    item.text(),
                )
            )

    def _get_additional_columns(self, primary_item, is_leaf, columns):
        items = []
        if is_leaf and columns:
            data = get_sg_data(primary_item)
            for column in columns:
                column_item = ShotgunStandardItem(
                    self.__generate_display_name(column, data)
                )
                column_item.setEditable(column in self.__editable_fields)
                value = data.get(column)
                column_item.setData(
                    sanitize_for_qt_model(value), self.SG_ASSOCIATED_FIELD_ROLE
                )
                items.append(column_item)
        return items

    def _get_additional_column_headers(self, entity_type, columns):
        return [
            self._shotgun_globals.get_field_display_name(entity_type, c)
            for c in columns
        ]

    def _get_columns(self, item, is_leaf):
        row = [item]
        row.extend(self._get_additional_columns(item, is_leaf, self.__column_fields))
        return row

    def _create_item(self, parent, data_item, top_index=None):
        item = ShotgunStandardItem()
        item.setEditable(data_item.field in self.__editable_fields)
        self._update_item(item, data_item)
        self._finalize_item(item)
        row = self._get_columns(item, data_item.is_leaf())
        if top_index is not None:
            parent.insertRow(top_index, row)
        else:
            parent.appendRow(row)
        return item

    def _update_item(self, item, data_item):
        try:
            field_display_name = self.__generate_display_name(
                data_item.field, data_item.shotgun_data
            )
            item.setText(field_display_name)
            item.setData(True, self.IS_SG_MODEL_ROLE)
            item.setData(not data_item.is_leaf(), self._SG_ITEM_HAS_CHILDREN)
            item.setData(data_item.unique_id, self._SG_ITEM_UNIQUE_ID)
            item.setData(
                {"name": data_item.field, "value": data_item.shotgun_data[data_item.field]},
                self.SG_ASSOCIATED_FIELD_ROLE
            )
            if data_item.is_leaf():
                item.setData(
                    sanitize_for_qt_model(data_item.shotgun_data), self.SG_DATA_ROLE
                )
            self._item_created(item)
            self._populate_default_thumbnail(item)
            if data_item.is_leaf():
                self._populate_item(item, data_item.shotgun_data)
            else:
                self._populate_item(item, None)
            self._set_tooltip(item, data_item.shotgun_data)
        except Exception as e:
            self._log_debug(f"Unable to update item, error: {e}")

    def _get_perforce_connection(self):
        """
        Retrieve Perforce connection from the framework.
        """
        try:
            fw = sgtk.platform.get_framework("tk-framework-perforce")
            return fw.connection.connect()
        except Exception as e:
            self._log_debug(f"Failed to connect to Perforce: {e}")
            return None

    def __compute_cache_path(self, cache_seed=None):
        params_hash = hashlib.md5()
        params_hash.update(str(self.__schema_generation).encode("utf-8"))
        params_hash.update(str(self.__fields).encode("utf-8"))
        params_hash.update(str(self.__order).encode("utf-8"))
        params_hash.update(str(self.__hierarchy).encode("utf-8"))
        if QtCore.Qt.UserRole != 32:
            params_hash.update(str(QtCore.Qt.UserRole).encode("utf-8"))
        filter_hash = hashlib.md5()
        filter_hash.update(str(self.__filters).encode("utf-8"))
        filter_hash.update(str(self.__additional_filter_presets).encode("utf-8"))
        params_hash.update(str(cache_seed).encode("utf-8"))
        if hasattr(self._bundle, "site_cache_location"):
            cache_location = self._bundle.site_cache_location
        else:
            cache_location = self._bundle.cache_location
        data_cache_path = os.path.join(
            cache_location,
            "sg",
            self.__entity_type,
            params_hash.hexdigest(),
            "%s.%s" % (filter_hash.hexdigest(), ShotgunFindDataHandler.FORMAT_VERSION)
        )
        if sgtk.util.is_windows() and len(data_cache_path) > 250:
            self._log_warning(
                "Flow Production Tracking model data cache file path may be affected by windows "
                "MAX_PATH limitation."
            )
        return data_cache_path

    def __generate_display_name(self, field, sg_data):
        value = sg_data.get(field)
        if isinstance(value, dict) and "name" in value and "type" in value:
            return value["name"] if value["name"] is not None else "Unnamed"
        elif isinstance(value, list):
            formatted_values = []
            if len(value) == 0:
                formatted_values.append("No Value")
            for v in value:
                if isinstance(v, dict) and "name" in v and "type" in v:
                    if v.get("name"):
                        formatted_values.append(v.get("name"))
                else:
                    formatted_values.append(str(v))
            return ", ".join(formatted_values)
        elif value is None:
            return "Unnamed"
        else:
            return str(value)