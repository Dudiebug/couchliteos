-- CouchLiteOS: sound follows the TV.
--
-- On a plain Intel/AMD HDA card (no UCM), PipeWire offers the analog jack and
-- each HDMI/DisplayPort output as separate card profiles, and only one can be
-- active. The analog profile has the higher priority and, because a built-in
-- speaker has no jack to report "unplugged", it always counts as available. So
-- WirePlumber's device/find-best-profile keeps the card on analog and the
-- HDMI/DP sink never exists, even with a TV attached.
--
-- This hook runs before WirePlumber's own choice: when an HDMI/DP profile is
-- available (its jack has a TV or monitor with speakers, a valid ELD), choose
-- the highest-priority one. A profile the user picked (stored by
-- device/state-profile, found by device/find-stored-profile) still wins, and
-- with no TV attached WirePlumber falls back to its usual choice. ACP updates
-- EnumProfile when a jack changes, so this runs again on hotplug.

cutils = require ("common-utils")
log = Log.open_topic ("s-device")

SimpleEventHook {
  name = "device/couchliteos-find-tv-profile",
  after = "device/find-stored-profile",
  before = "device/find-preferred-profile",
  interests = {
    EventInterest {
      Constraint { "event.type", "=", "select-profile" },
    },
  },
  execute = function (event)
    -- a stored (user-chosen) profile was already found
    if event:get_data ("selected-profile") then
      return
    end

    local device = event:get_subject ()
    if device.properties ["device.api"] ~= "alsa" then
      return
    end

    local dev_name = device.properties ["device.name"] or ""
    local tv_profile = nil
    local hdmi_profiles = 0
    for p in device:iterate_params ("EnumProfile") do
      local profile = cutils.parseParam (p, "EnumProfile")
      if profile and string.find (profile.name, "^output:hdmi%-") then
        hdmi_profiles = hdmi_profiles + 1
        if profile.available == "yes"
            and (tv_profile == nil or profile.priority > tv_profile.priority) then
          tv_profile = profile
        end
      end
    end

    -- notice level: shown by default, so it lands in audio.log and support files
    if tv_profile then
      log:notice (device, string.format ("couchliteos: TV profile '%s' (%d) for '%s'",
          tv_profile.name, tv_profile.index, dev_name))
      event:set_data ("selected-profile", tv_profile)
    elseif hdmi_profiles > 0 then
      log:notice (device, string.format (
          "couchliteos: no TV on the %d HDMI/DP profile(s) of '%s'", hdmi_profiles, dev_name))
    end
  end
}:register ()
