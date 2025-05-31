# Copyright (c) 2016 Shotgun Software Inc.
#
# CONFIDENTIAL AND PROPRIETARY
#
# This work is provided "AS IS" and subject to the Shotgun Pipeline Toolkit
# Source Code License included in this distribution package. See LICENSE.
# By accessing, using, copying or modifying this work you indicate your
# agreement to the Shotgun Pipeline Toolkit Source Code License. All rights
# not expressly granted therein are reserved by Shotgun Software Inc.
import gc

from .data_handler import ShotgunDataHandler
from .errors import ShotgunModelDataError
from .data_handler_cache import ShotgunDataHandlerCache
from .util import compare_shotgun_data
import sgtk
from collections import defaultdict
import os

from sgtk.platform.qt import QtCore
for name, cls in QtCore.__dict__.items():
    if isinstance(cls, type): globals()[name] = cls

from sgtk.platform.qt import QtGui
for name, cls in QtGui.__dict__.items():
    if isinstance(cls, type): globals()[name] = cls

class ShotgunFindDataHandler(ShotgunDataHandler):
    """
    Shotgun Model low level data storage for use
    with the Shotgun Model.

    This implements a data storage where a single
    shotgun find query is stored in the cache file.
    """

    def __init__(
        self,
        entity_type,
        filters,
        order,
        hierarchy,
        fields,
        download_thumbs,
        limit,
        additional_filter_presets,
        cache_path,
        sg_data_type = None,
    ):
        """
        :param entity_type:               Shotgun entity type to download
        :param filters:                   List of Shotgun filters. Standard Shotgun syntax.
        :param order:                     Order clause for the Shotgun data. Standard Shotgun API syntax.
        :param hierarchy:                 List of grouping fields. These should be names of Shotgun
                                          fields. If you for example want to create a list of items,
                                          the value ``["code"]`` will be suitable. This will generate a data
                                          model which is flat and where each item's default name is the
                                          Shotgun name field. If you want to generate a tree where assets
                                          are broken down by asset type, you could instead specify
                                          ``["sg_asset_type", "code"]``.
        :param fields:                    List of field names to retrieve from Shotgun (in addition to
                                          the ones specified in the hierarchy parameter).
        :param download_thumbs:           Boolean to indicate if this model should attempt
                                          to download and process thumbnails for the downloaded data.
        :param limit:                     Limit the number of results returned from Shotgun. In conjunction with the
                                          order
                                          parameter, this can be used to effectively cap the data set that the model
                                          is handling, allowing a user to for example show the twenty most recent note
                                          or similar.
        :param additional_filter_presets: List of Shotgun filter presets to apply, e.g.
                                          ``[{"preset_name":"LATEST","latest_by":"BY_PIPELINE_STEP_NUMBER_AND_ENTITIES_CREATED_AT"}]``
        :param cache_path:                Path to cache file location
        """
        super(ShotgunFindDataHandler, self).__init__(cache_path)
        self.__entity_type = entity_type
        self.__filters = filters
        self.__order = order
        self.__hierarchy = hierarchy
        self.__fields = fields
        self.__download_thumbs = download_thumbs
        self.__limit = limit
        self.__additional_filter_presets = additional_filter_presets
        self.__sg_data_type = sg_data_type
        self._sg_data = None
        self._p4 = None
        self._peforce_data = {}
        self.status_dict = {
            "add": "p4add",
            "move/add": "p4add",
            "delete": "p4del",
            "edit": "p4edit"
        }
        self.settings = {
            "wire": "Alias File",
            "abc": "Alembic Cache",
            "max": "3dsmax Scene",
            "hrox": "NukeStudio Project",
            "hip": "Houdini Scene",
            "hipnc": "Houdini Scene",
            "hiplc": "Houdini Scene",
            "ma": "Maya Scene",
            "mb": "Maya Scene",
            "fbx": "Motion Builder FBX",
            "nk": "Nuke Script",
            "psd": "Photoshop Image",
            "psb": "Photoshop Image",
            "vpb": "VRED Scene",
            "vpe": "VRED Scene",
            "osb": "VRED Scene",
            "dpx": "Rendered Image",
            "exr": "Rendered Image",
            "tiff": "Texture",
            "tx": "Texture",
            "tga": "Texture",
            "dds": "Texture",
            "jpeg": "Image",
            "jpg": "Image",
            "mov": "Movie",
            "mp4": "Movie",
            "pdf": "PDF"
        }

    def get_entity_ids(self):
        """
        Returns a list of entity ids contained in this data set given an entity type.

        Performs a sequential scan over all items in the data set.

        :return: A list of unique ids for all items in the model.
        :rtype: ``list``
        """
        if not self.is_cache_loaded():
            return []

        # loop over all cache items - the find data handler organizes
        # its unique ids so that all leaf nodes (e.g. representing an entity)
        # are ints and all other items are strings
        entity_ids = []
        for data_item in self._cache.get_all_items():
            if data_item.is_leaf():
                entity_ids.append(data_item.unique_id)

        return entity_ids

    def get_uid_from_entity_id(self, entity_id):
        """
        Returns the unique id for a given entity or None if not found.

        :param entity_id: Shotgun entity id to resolve
        :returns: unique id as string or int, to be used with
                  :meth:`get_data_item_from_uid`.
        """
        if not self.is_cache_loaded():
            return None

        # leaf nodes are keyed by entity id
        if self._cache.item_exists(entity_id):
            return entity_id
        else:
            return None

    def generate_data_request(self, data_retriever):
        """
        Generate a data request for a data retriever.

        Once the data has arrived, the caller is expected to
        call meth:`update_data` and pass in the received
        data payload for processing.

        :param data_retriever: :class:`~tk-framework-shotgunutils:shotgun_data.ShotgunDataRetriever` instance.
        :returns: Request id or None if no work is needed
        """
        # only request data from shotgun is filters are defined.
        if self.__filters is None:
            return None

        # get data from shotgun - list/set cast to ensure unique fields
        fields = self.__hierarchy + self.__fields
        if self.__download_thumbs:
            fields = fields + ["image"]
        fields = list(set(fields))

        find_kwargs = dict(limit=self.__limit)

        # We only want to include the filter presets kwarg if it was explicitly asked
        # for. The reason for this is that it's a Shotgun 7.0 feature server side, and
        # we don't want to break backwards compatibility with older versions of Shotgun.
        if self.__additional_filter_presets:
            find_kwargs["additional_filter_presets"] = self.__additional_filter_presets

        request_id = data_retriever.execute_find(
            self.__entity_type, self.__filters, fields, self.__order, **find_kwargs
        )

        return request_id

    @sgtk.LogManager.log_timing
    def update_data(self, sg_data, p4_connection = None):
        """
        The counterpart to :meth:`generate_data_request`. When the data
        request has been carried out, this method should be called by the calling
        class and the data payload from Shotgun should be provided via the
        sg_data parameter.

        The shotgun find data is compared against the existing tree and
        a list of differences is returned, indicating which nodes were
        added, deleted and modified, on the following form::

            [
             {
                "data": ShotgunItemData instance,
                "mode": self.UPDATED|ADDED|DELETED
             },
             {
                "data": ShotgunItemData instance,
                "mode": self.UPDATED|ADDED|DELETED
             },
             ...
            ]

        :param sg_data: list, resulting from a Shotgun find query
        :returns: list of updates. see above
        :raises: :class:`ShotgunModelDataError` if no cache is loaded into memory
        """
        self._p4 = p4_connection
        self._log_debug("Updating %s with %s shotgun records." % (self, len(sg_data)))
        self._log_debug("Hierarchy: %s" % self.__hierarchy)

        new_cache = ShotgunDataHandlerCache()

        # If we don't already have a cache, we can continue on here by just populating
        # our new cache with the necessary data.
        if self._cache is None:
            self.load_cache()

        if self._cache.size == 0:
            self._log_debug("In-memory cache is empty.")

        # ensure the data is clean
        self._log_debug("sanitizing data...")
        sg_data = self._sg_clean_data(sg_data)
        self._log_debug("...done!")


        # Get perforce data
        self._log_debug(">>>> data_handler_find->update_date: sg data type is {}".format(self.__sg_data_type))
        #self._log_debug(f">>>> sg_data type is {self.__sg_data_type}")
        #self._log_debug(f">>>> sg_data is {sg_data}")
        #if self.__sg_data_type == "Asset" or self.__sg_data_type == "Task":
        if sg_data:

            if not self._p4:
                self._connect_P4()
            sg_data = self._get_peforce_data(sg_data)

        self._log_debug("Generating new tree in memory...")

        # create a brand new tree rather than trying to be clever
        # about how we cull intermediate nodes for deleted items
        diff_list = []
        num_adds = 0
        num_deletes = 0
        num_modifications = 0

        # analyze the incoming shotgun data
        for sg_item in sg_data:

            parent_uid = None

            # Create items by drilling down the hierarchy
            for field_name in self.__hierarchy:

                on_leaf_level = self.__hierarchy[-1] == field_name

                if not on_leaf_level:
                    # generate path for this item
                    unique_field_value = self.__generate_unique_key(
                        parent_uid, field_name, sg_item
                    )
                else:
                    # on the leaf level, use the entity id as the unique key
                    unique_field_value = sg_item["id"]

                # two distinct cases for leaves and non-leaves
                if on_leaf_level:
                    # this is an actual entity - insert into our new tree
                    new_cache.add_item(
                        parent_uid, sg_item, field_name, True, unique_field_value
                    )

                    # now check with prev data structure to see if it has changed
                    if not self._cache.item_exists(unique_field_value):
                        # this is a new node that wasn't there before
                        diff_list.append(
                            {
                                "data": new_cache.get_entry_by_uid(unique_field_value),
                                "mode": self.ADDED,
                            }
                        )
                        num_adds += 1
                    else:
                        # record already existed in prev dataset. Check if value has changed
                        old_record = self._cache.get_shotgun_data(unique_field_value)
                        if not compare_shotgun_data(old_record, sg_item):
                            diff_list.append(
                                {
                                    "data": new_cache.get_entry_by_uid(
                                        unique_field_value
                                    ),
                                    "mode": self.UPDATED,
                                }
                            )
                            num_modifications += 1

                else:
                    # not on leaf level yet
                    if not new_cache.item_exists(unique_field_value):
                        # item is not yet inserted in our new tree so add it
                        # because these are parent items like project nodes
                        new_cache.add_item(
                            parent_uid, sg_item, field_name, False, unique_field_value
                        )

                        # now check with prev data structure to see if it has changed
                        if not self._cache.item_exists(unique_field_value):
                            # this is a new node that wasn't there before
                            diff_list.append(
                                {
                                    "data": new_cache.get_entry_by_uid(
                                        unique_field_value
                                    ),
                                    "mode": self.ADDED,
                                }
                            )
                            num_adds += 1
                        else:
                            # record already existed in prev dataset. Check if value has changed
                            current_record = self._cache.get_shotgun_data(
                                unique_field_value
                            )
                            # don't compare the whole record but just the part that relates to this
                            # intermediate node value. For example, we may be looking at a project node
                            # in the hierarchy but the full sg record contains all the data for a shot.
                            # in this case, just run the comparison on the project subset of the full
                            # shot data dict.
                            if not compare_shotgun_data(
                                current_record.get(field_name), sg_item.get(field_name)
                            ):
                                diff_list.append(
                                    {
                                        "data": self._cache.get_entry_by_uid(
                                            unique_field_value
                                        ),
                                        "mode": self.UPDATED,
                                    }
                                )
                                num_modifications += 1

                    # recurse down to the next level
                    parent_uid = unique_field_value

        # now figure out if anything has been removed
        self._log_debug("Diffing new tree against old tree...")

        current_uids = set(self._cache.uids)
        new_uids = set(new_cache.uids)

        for deleted_uid in current_uids.difference(new_uids):
            diff_list.append(
                {
                    "data": self._cache.get_entry_by_uid(deleted_uid),
                    "mode": self.DELETED,
                }
            )
            num_deletes += 1

        # Lastly, swap in the new cache
        self._cache = None

        # at this point, kick the gc to make sure the memory is freed up
        # despite its cycles.
        gc.collect()

        # and set the new cache
        self._cache = new_cache

        self._log_debug(
            "Flow Production Tracking data (%d records) received and processed. "
            % len(sg_data)
        )
        self._log_debug("    The new tree is %d records." % self._cache.size)
        self._log_debug(
            "    There were %d diffs from in-memory cache:" % len(diff_list)
        )
        self._log_debug("    Number of new records: %d" % num_adds)
        self._log_debug("    Number of deleted records: %d" % num_deletes)
        self._log_debug("    Number of modified records: %d" % num_modifications)

        return diff_list

    def _get_peforce_data(self, sg_data):
        if sg_data:
            """
            if len(sg_data) <= 1:
                self._log_debug(">>>>>>>>>>  Processing small data")
                sg_data = self._get_small_peforce_data(sg_data)
            else:
            """
            self._log_debug(">>>>>>>>>>  Processing large data")
            sg_data = self._get_large_peforce_data(sg_data)
        return sg_data

    def _get_large_peforce_data(self, sg_data):
        """"
        Get large perforce data
        """

        """
        if sg_data:
            # Step 1: Filter sg_data to keep only the highest version_number for each file
            filtered_sg_data = []
            file_version_map = defaultdict(list)

            # Group sg_data by file name (local_path)
            for sg_item in sg_data:
                sg_item_path = sg_item.get("path", {})
                local_path = sg_item_path.get("local_path", None)
                if local_path:
                    file_version_map[local_path].append(sg_item)

            # Keep only the item with the highest version_number for each file
            for local_path, items in file_version_map.items():
                if len(items) > 1:
                    # Sort by version_number (descending) and take the first item
                    highest_version_item = max(
                        items,
                        key=lambda x: x.get("version_number", 0),
                        default=items[0]
                    )
                    filtered_sg_data.append(highest_version_item)
                    self._log_debug(
                        f"Keeping highest version for {local_path}: version_number={highest_version_item.get('version_number', 0)}"
                    )
                else:
                    filtered_sg_data.append(items[0])

            sg_data = filtered_sg_data
        """
        item_path_dict = defaultdict(int)
        fstat_dict = {}
        if sg_data:
            for i, sg_item in enumerate(sg_data):
                #if i == 0:
                #    self._log_debug("sg_item is: {}".format(sg_item))
                sg_item_path = sg_item.get("path", None)
                if sg_item_path:
                    if "local_path" in sg_item_path:
                        local_path = sg_item_path.get("local_path", None)

                        if local_path:
                            # self._log_debug("local_path is: {}".format(local_path))
                            # item_path = self._get_item_path(local_path)
                            item_path = os.path.dirname(local_path)
                            item_path_dict[item_path] += 1
            #self._log_debug(">>>>>>>>>>  item_path_dict is: {}".format(item_path_dict))

            for key in item_path_dict:
                if key:
                    # self._log_debug(">>>>>>>>>>  key is: {}".format(key))
                    #key = "{}\\...".format(key)
                    key = key.rstrip('/')
                    # self._log_debug("^^^ key is: {}".format(key))
                    fstat_list = self._p4.run_fstat('-Of', key + '/...')
                    # fstat_list = self._p4.run("fstat", key)
                    for i, fstat in enumerate(fstat_list):
                        client_file = fstat.get('clientFile', None)
                        # if i == 0:
                        #    self._log_debug(">>>>>>>>>>  client_file is: {}".format(client_file))
                        if client_file:
                            have_rev = fstat.get('haveRev', "0")
                            head_rev = fstat.get('headRev', "0")
                            key = self._create_key(client_file)
                            key = "{}#{}".format(key, head_rev)
                            if key not in fstat_dict:

                                fstat_dict[key] = fstat
                                fstat_dict[key]['Published'] = False
                                action = fstat.get('action', None)
                                if action:
                                    sg_status = self._get_p4_status(action)
                                    if sg_status:
                                        fstat_dict[key]['sg_status_list'] = sg_status

                                # if i == 0:
                                #     self._log_debug(">>>>>>>>>>  fstat_dict[client_file] is: {}".format(fstat_dict[modified_client_file]))
            # self._log_debug(">>>>>>>>>>  fstat_dict is: {}".format(fstat_dict))

            for i, sg_item in enumerate(sg_data):
                sg_item_path = sg_item.get("path", None)
                if sg_item_path:
                    local_path = sg_item_path.get("local_path", None)
                    if local_path:
                        key = self._create_key(local_path)
                        version_number = sg_item.get("version_number", None)
                        # Get the version number from the path if it exists
                        if version_number:
                            version_number = int(version_number)
                            key = "{}#{}".format(key, version_number)

                            if key and key in fstat_dict:

                                have_rev = fstat_dict[key].get('haveRev', "0")
                                head_rev = fstat_dict[key].get('headRev', "0")
                                fstat_dict[key]['Published'] = True

                                sg_item["haveRev"], sg_item["headRev"] = have_rev, head_rev
                                sg_item["revision"] = "{}/{}".format(have_rev, head_rev)
                                sg_item["headAction"] = fstat_dict[key].get('headAction', None)
                                sg_item["action"] = fstat_dict[key].get('action', None)
                                sg_item["depotFile"] = fstat_dict[key].get('depotFile', None)
                                sg_item["sg_status_list"] = fstat_dict[key].get('sg_status_list', None)
                                sg_item["headModTime"] = fstat_dict[key].get('headModTime', None)
                                sg_item["actionOwner"] = fstat_dict[key].get('actionOwner', None)
                                sg_item["headTime"] = fstat_dict[key].get('headTime', None)
                                sg_item["sg_p4_user"] = fstat_dict[key].get('sg_p4_user', None)
                                #sg_item["newAction"] = sg_item["headAction"]
                                # sg_item["newAction"] = None
                                #if sg_item.get("version_number", 0) == 0:
                                #    sg_item["version_number"] = int(head_rev)
            """
            query = "Helena_LC_BangsReduced_TEK"
            self._log_debug("------------------------------------------------------------------------------------")
            for i, sg_item in enumerate(sg_data):
                sg_item_path = sg_item.get("path", None)
                local_path = sg_item_path.get("local_path", None)

                if query in local_path:
                    self._log_debug("-------------------------------")
                    self._log_debug("Found query: {} in local_path: {}".format(query, local_path))
                    for key, value in sg_item.items():
                        self._log_debug(">>> {}: {}".format(key, value))

                else:
                    # self._log_debug("could not find query: {} in local_path: {}".format(query, local_path))
                    continue
            self._log_debug("------------------------------------------------------------------------------------")
            self._log_debug(">>>>>>>>>>fstat_dict:")
            for k, fstat in fstat_dict.items():
                depot_file = fstat_dict[k].get('depotFile', None)
                if query in depot_file:
                    self._log_debug("Found query: {} in depot_file: {}".format(query, depot_file))
                    self._log_debug(">>> {}: {}".format(k, fstat))
                    self._log_debug(">>>>>>>>>>fstat ")
                    for key, value in fstat.items():
                        self._log_debug(">>> {}: {}".format(key, value))
            

            repo_root = os.path.normpath(
                os.path.join(os.path.dirname(__file__), "..", "..")
            )
            self._log_debug(">>>>>>>>>> repo_root: {}".format(repo_root))
            perforce_publish_image_path = os.path.join(repo_root, "icons/perforce_1.png")
            self._log_debug(">>>>>>>>>> perforce_publish_image_path: {}".format(perforce_publish_image_path))
            perforce_publish_icon = QIcon(QPixmap(perforce_publish_image_path))
            """

            """
            # Removed because they are not linked to asset
            id = 9999000

            # Step 1: Find the highest headRev for each unique local_path
            highest_revs = {}
            fstat_by_local_path = {}
            for key, fstat in fstat_dict.items():
                depot_file = fstat.get("depotFile", "")
                local_path = fstat.get("clientFile", "")
                head_rev = int(fstat.get("headRev", 0))
                if local_path:
                    if local_path not in highest_revs or head_rev > highest_revs[local_path]["head_rev"]:
                        highest_revs[local_path] = {"head_rev": head_rev, "key": key}
                        fstat_by_local_path[local_path] = fstat

            # Step 2: Process only the highest revision files
            for local_path, fstat in fstat_by_local_path.items():
                key = highest_revs[local_path]["key"]
                head_rev = highest_revs[local_path]["head_rev"]
                if not fstat.get('Published', False):
                    new_sg_item = fstat
                    new_sg_item["source"] = "Perforce"
                    new_sg_item["type"] = "Asset"
                    new_sg_item["sg_status_list"] = "Needs Publishing"
                    new_sg_item["entity"] = {'id': 14456, 'name': 'BigPalm_SM_Jackson', 'type': 'Asset'}
                    new_sg_item["id"] = id
                    id += 1
                    new_sg_item["path"] = {"local_path": local_path}
                    file_base_name = os.path.basename(local_path)
                    new_sg_item["name"] = file_base_name
                    have_rev = fstat.get('haveRev', "0")
                    new_sg_item["revision"] = "{}/{}".format(have_rev, head_rev)
                    new_sg_item["code"] = "{}#{}".format(file_base_name, head_rev)
                    file_type_name = self._get_publish_type(file_base_name)
                    new_sg_item["file_type_name"] = file_type_name
                    new_sg_item["description"] = fstat.get('desc', None)
                    new_sg_item["created_at"] = fstat.get('headTime', None)
                    new_sg_item["sg_p4_depo_path"] = fstat.get('depotFile', None)
                    sg_data.append(new_sg_item)
            # END OF "Removed because they are not linked to asset"
                    
            """

            """
            id = 9999000

            # Step 1: Find the highest headRev for each unique file
            highest_revs = {}
            for key, fstat in fstat_dict.items():
                depot_file = fstat.get("depotFile", "")
                head_rev = int(fstat.get("headRev", 0))

                if depot_file:
                    if depot_file not in highest_revs or head_rev > highest_revs[depot_file]:
                        highest_revs[depot_file] = head_rev

            # Step 2: Process only the highest revision files

            for key, fstat in fstat_dict.items():
                depot_file = fstat.get("depotFile", "")
                head_rev = int(fstat.get("headRev", 0))

                if depot_file and head_rev == highest_revs.get(depot_file):
                    if not fstat.get('Published', False):

                        new_sg_item = fstat
                        # new_sg_item["Published"] = True
                        new_sg_item["source"] = "Perforce"
                        # new_sg_item["type"] = "Perforce"
                        new_sg_item["type"] = "Asset"
                        # new_sg_item["type"] = "depotFile"
                        new_sg_item["sg_status_list"] = "Needs Publishing"
                        new_sg_item["entity"] = {'id': 14456, 'name': 'BigPalm_SM_Jackson', 'type': 'Asset'}
                        id += 1
                        new_sg_item["id"] = id

                        new_sg_item["path"] = {"local_path": fstat.get('clientFile', None)}
                        # new_sg_item["path"] = {"local_path": fstat.get('depotFile', None)}
                        file_base_name = os.path.basename(fstat.get('clientFile', None))
                        new_sg_item["name"] = file_base_name
                        head_rev = fstat.get('headRev', "0")
                        have_rev = fstat.get('haveRev', "0")
                        new_sg_item["revision"] = "{}/{}".format(have_rev, head_rev)
                        #new_sg_item["version_number"] = int(head_rev)
                        new_sg_item["code"] = "{}#{}".format(file_base_name, head_rev)
                        file_type_name = self._get_publish_type(file_base_name)
                        new_sg_item["file_type_name"] = file_type_name
                        id += 1
                        #new_sg_item["published_file_type"] = {'id': id, 'name': file_type_name,'type': 'PublishedFileType'}

                        new_sg_item["description"] = fstat.get('desc', None)
                        new_sg_item["created_at"] = fstat.get('headTime', None)
                        #new_sg_item["project"] = self._entity.get("project", None) if self._entity else None
                        #new_sg_item["task"] = self._entity.get("task", None) if self._entity else None
                        new_sg_item["sg_p4_depo_path"] = fstat.get('depotFile', None)

                        #new_sg_item["image"] = self.get_perforce_image()

                        # self._log_debug(">>>>>>>>>> new_sg_item: {}".format(new_sg_item))
                        sg_data.append(new_sg_item)
            """
            """
            self._log_debug("--------------------------------------------------------------------------------")
            self._log_debug(">>>>>>>>>> sg_data: ")
            for i, sg_item in enumerate(sg_data):

                local_path = sg_item.get("path", {}).get("local_path", "")
                if query in local_path:
                    self._log_debug("-------------------------------")

                    self._log_debug(">>> FOUND query: {} in local_path: {}".format(query, local_path))
                    for key, value in sg_item.items():
                        self._log_debug(">>> {}: {}".format(key, value))

            
            self._log_debug("--------------------------------------------------------------------------------")
            self._log_debug(">>>>>>>>>> sg_data: ")
            for i, sg_item in enumerate(sg_data):
                self._log_debug("-------------------------------")
                for key, value in sg_item.items():
                    self._log_debug(">>> {}: {}".format(key, value))
            """





        return sg_data

    def _get_file_log(self, file_path):
        try:
            filelog_list = self._p4.run("filelog", "-l", file_path)
            # self._log_debug(">>>>>> filelog_list: {}".format(filelog_list))
            if filelog_list:
                filelog = filelog_list[0]
                # 'desc': ['- Climb Idle ']
                desc = filelog.get("desc", None)
                if desc:
                    desc = desc[0]
                    if desc.startswith("-"):
                        desc = desc[1:]
                    if desc.startswith(" "):
                        desc = desc[1:]
                # 'user': ['michael']
                user = filelog.get("user", None)
                if user:
                    user = user[0]
                    user = user.capitalize()
                return desc, user
            else:
                return None, None
        except:
            return None, None

    def get_perforce_image(self):
        img_4 = "https://sg-media-usor-01.s3-accelerate.amazonaws.com/40340ec70ef011c71c9f7a45c41efa8a9a477c61/1569a65a2b157f3ebadac901febefa06ad36eae4/perforce_4.v1.png?response-content-disposition=filename%3D%22perforce_4.v1.png%22&x-amz-meta-user-id=2299&x-amz-meta-user-type=HumanUser&X-Amz-Algorithm=AWS4-HMAC-SHA256&X-Amz-Credential=ASIAYJG6Z4JIZED5WIS5%2F20250224%2Fus-west-2%2Fs3%2Faws4_request&X-Amz-Date=20250224T174650Z&X-Amz-SignedHeaders=host&X-Amz-Expires=900&X-Amz-Security-Token=IQoJb3JpZ2luX2VjEPr%2F%2F%2F%2F%2F%2F%2F%2F%2F%2FwEaCXVzLWVhc3QtMSJHMEUCIDfM%2BphumYao0aZsRC0xT4MLGgPXSN1Qgu32CLmv%2B%2FskAiEA4r3qPquOy%2FPD53laUUzGXb1obVjwXhz2FW0VlEbSssIqmAIIMxAAGgw1Njk1NTA0MzA4MDEiDCXnX5kZyyOel3Q%2Bpir1AV35Xrf8Wu46fPGy6KFshGlHhFdSq%2F9J%2F3EFhy2X1jXNY%2B3PAG0nNvDwo2kirVIWsy%2FKlbtVxZKPJFTTRBv8QdkDK%2Bc5BM6oqG36BS5u%2F8h2kn7YILHLGKwZlrnTKhsYB7GahcgMcBRfs1u4uZvhWgPASPp3PwfWisn8V51kDMfFt9MQP96xQ550t8k0zg%2BzaYOlpqFp0h9s0MYnOtWgqygOtP6Wg7jLWWpghz6B%2FuPhela4cu%2Bc%2BJh0J0suHx9swSZU0OBNy1wIbaWgg1JUCB0M7K2RbD4O9ErMQGC%2Bt%2BLaKz2VmX%2FW%2FmvcUdgdC1S6oZ3MzBWuML7c8r0GOp0B2Fw8wvzGr%2Fi4R6SFEZBDHR9fM4ICDO0F6KzOsQCGF14u5hdOhJ9lySnNa%2F3U2m1Fq1Q%2BMMreF2mL%2Fiy8JmckPXouUSGQVaukZ8wMKxQ1bB6BfSLA8%2FfI3nFGtyvIF3cFs8UnM8GVu3A7StyjkgIldh64vPkcV47qOxcyRvd1JySEwUKKptKLNOPIpu%2BGH2aStBUpeRYlIvFTfmHxBA%3D%3D&X-Amz-Signature=6d497476ad12670be9ef10907606d13d628553511535f2285604a793ca307d30"
        img_5 = "https://sg-media-usor-01.s3-accelerate.amazonaws.com/40340ec70ef011c71c9f7a45c41efa8a9a477c61/f04efa4b3d82b9f7bf76b4ea340bc7049bcaaf66/perforce_5.v1.png?response-content-disposition=filename%3D%22perforce_5.v1.png%22&x-amz-meta-user-id=2299&x-amz-meta-user-type=HumanUser&X-Amz-Algorithm=AWS4-HMAC-SHA256&X-Amz-Credential=ASIAYJG6Z4JIUXWVQ3JX%2F20250224%2Fus-west-2%2Fs3%2Faws4_request&X-Amz-Date=20250224T200234Z&X-Amz-SignedHeaders=host&X-Amz-Expires=900&X-Amz-Security-Token=IQoJb3JpZ2luX2VjEPz%2F%2F%2F%2F%2F%2F%2F%2F%2F%2FwEaCXVzLWVhc3QtMSJIMEYCIQDS%2BqPRuacn2i7aaetoPPQNnqFj0VWKkDGACK7yAfpOagIhAPlbVrLu%2FI1tU7gLUbBu00iFyld7JZCbeuebf33nDDAiKpgCCDUQABoMNTY5NTUwNDMwODAxIgzuRcI%2BgesR%2BcenExEq9QGp4pMXgZzOzyhxaOHTbNtVfmWUscP8Y02gt9NaP2XUM7MKZkWDzSrOfKLBuDzva2q10UMBPHuTZg0wi7YhkNEh9XN5P22BzLW6Gq9cj2Py9geP2w2Te1Sf6Yovabdy9J2HTjMK1eTyGk78P6Sjffrw8Z%2FVworGa5PUeoeFg8IvCtHgQCoVmloDYqELdoYLvXPEBLw2HyYcpzxPdOOH1E81YDSfmHG5%2FZv1HacpysbsTi%2F2z19Ed6X5A46MG14Po3uIwWSeX4%2FMJavLeZ4SZZTYZSfrpGIu%2BO%2FWvNMJHy6HRfVJ5gUvW3dxrk43VrKbZvQQ1kH6ODCllfO9BjqcAdjPlK3OiEVjA2VF6R3%2B2Kl36UofsC7lcmAptfgVNWlKIikKEtVCxRlIhPxt1mjXq8Em%2FR3W6r7wxF1NtW2hZbHIRemdGBx9qJneVdNp8rtWLmgLQe59DSHkumpt32DcUimdZgNSWEKkT0nx6sGM87Fn3nIW79%2B1KqGwPhdDEq8l96Qo0QDXQPeC6Jiu372Sln8HzMIQuAuacPT%2Fxg%3D%3D&X-Amz-Signature=76f34b4b48dc72e3211d9527cd959508cbb315ff69e81bf818600e1f3d57f268"
        return img_5
    def _get_publish_type(self, publish_path):
        """
        Get a publish type
        """
        publish_type = None
        publish_path = os.path.splitext(publish_path)
        if len(publish_path) >= 2:
            extension = publish_path[1]

            # ensure lowercase and no dot
            if extension:
                extension = extension.lstrip(".").lower()
                publish_type = self.settings.get(extension, None)
                if not publish_type:
                    # publish type is based on extension
                    publish_type = "%s File" % extension.capitalize()
            else:
                # no extension, assume it is a folder
                publish_type = "Folder"
        return publish_type

    def _get_p4_status(self, p4_status):

        p4_status = p4_status.lower()
        sg_status = self.status_dict.get(p4_status, None)
        # self._log_debug("p4_status: {}".format(p4_status))
        # self._log_debug("sg_status: {}".format(sg_status))
        return sg_status

    def _create_key(self, file_path):
        if file_path:
            file_path = file_path.replace("\\", "")
            file_path = file_path.replace("/", "")
            file_path = file_path.lower()
        return file_path


    def _get_item_path (self, local_path):
        """
        Get item path
        """
        item_path = ""
        if local_path:
            local_path = local_path.split("\\")
            local_path = local_path[:7]
            item_path = "\\".join(local_path)
        return item_path

    def _get_small_peforce_data(self, sg_data):
        """"
        Get small perforce data
        """

        if sg_data:
            for i, sg_item in enumerate(sg_data):
                if "path" in sg_item:
                    local_path = sg_item["path"].get("local_path", None)

                    # self._log_debug(">>>>>>> local_path is: {}".format(local_path))
                    if local_path:
                        fstat_list = self._p4.run("fstat", local_path)
                        # self._log_debug("fstat_list: {}".format(fstat_list))
                        fstat = fstat_list[0]
                        # self._log_debug("fstat is: {}".format(fstat))
                        have_rev = fstat.get('haveRev', "0")
                        head_rev = fstat.get('headRev', "0")
                        sg_item["haveRev"], sg_item["headRev"] = have_rev, head_rev
                        sg_item["revision"] = "{}/{}".format(have_rev, head_rev )
                        # self._log_debug("{}: Revision: {}".format(i, sg_item["revision"]))
                        # sg_item['depotFile'] = fstat.get('depotFile', None)

            # self._log_debug("{}: SG item: {}".format(i, sg_item))

        return sg_data

    def _get_latest_revision(self, files_to_sync):
        for file_path in files_to_sync:
            p4_result = self._p4.run("sync", "-f", file_path + "#head")
            self._log_debug("Syncing file: {}".format(file_path))

    def _to_sync (self, have_rev, head_rev):
        """
        Determine if we should sync the file
        """
        have_rev_int = int(have_rev)
        head_rev_int = int(head_rev)
        if head_rev_int > 0 and have_rev_int < head_rev_int:
            return True
        return False

    def _get_depot_path(self, local_path):
        """
        Convert local path to depot path
        For example, convert: 'B:\\Ark2Depot\\Content\\Base\\Characters\\Human\\Survivor\\Armor\\Cloth_T3\\_ven\\MDL\\Survivor_M_Armor_Cloth_T3_MDL.fbx'
        to "//Ark2Depot/Content/Base/Characters/Human/Survivor/Armor/Cloth_T3/_ven/MDL/Survivor_M_Armor_Cloth_T3_MDL.fbx"
        """
        local_path = local_path[2:]
        depot_path = local_path.replace("\\", "/")
        depot_path = "/{}".format(depot_path)
        return depot_path

    def _connect_P4(self):
        """
        Connect to Perforce.  If a connection can't be established with
        the current settings then the connection UI will be shown.
        """
        try:
            if not self._p4:
                self._log_debug("Connecting to perforce ...")
                self._fw = sgtk.platform.get_framework("tk-framework-perforce")
                self._p4 = self._fw.connection.connect()

        except:
            #Todo add error message
            self._log_debug("Failed to connect!")
            raise

    def _get_data(self):
        return self._sg_data

    def __generate_unique_key(self, parent_unique_key, field, sg_data):
        """
        Generates a unique key from a shotgun field.

        Used in conjunction with the hierarchical nature of the find data handler.

        :param parent_unique_key: uid for the parent
        :param field: the shotgun field that the node represents
        :param sg_data: associated shotgun data dictionary
        :returns: Unique string or int
        """
        # note: these ids are written to disk and kept in memory
        # and thus affect memory usage and i/o peformance. We assume
        # that a vast majority of nodes are leaves and store these as
        # ints for compactness. non-leaf nodes have a "path-like" string
        # to uniquely describe their location in the tree.
        #
        # we assume that on each level, values are unique.

        value = sg_data.get(field)

        if isinstance(value, dict) and "id" in value and "type" in value:
            # for single entity links, return the entity id
            unique_key = "%s_%s" % (value["type"], value["id"])

        elif isinstance(value, list):
            # this is a list of some sort. Loop over all elements and extract a comma separated list.
            formatted_values = []
            for v in value:
                if isinstance(v, dict) and "id" in v and "type" in v:
                    # This is a link field
                    formatted_values.append("%s_%s" % (v["type"], v["id"]))
                else:
                    formatted_values.append(str(v))
            unique_key = ",".join(formatted_values)

        else:
            # everything else just cast to string
            unique_key = str(value)

        if parent_unique_key is None:
            # no parent
            return "/%s" % unique_key
        else:
            return "%s/%s" % (parent_unique_key, unique_key)
