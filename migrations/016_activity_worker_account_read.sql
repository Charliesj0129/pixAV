-- Expand only. The managed storage worker signs a Google account into the
-- retained guest, so it has to read the leased account row the authority
-- addressed it with (pixel_injector/storage_activity.py::_account).
--
-- 013 granted accounts only to the execution authority, which is correct for
-- writes: quota is debited inside confirm_backup and must stay the authority's
-- exclusive act. This adds the read the worker actually performs and nothing
-- else, so an activity worker still cannot spend or restore quota.
GRANT SELECT ON accounts TO pixav_activity_worker;
