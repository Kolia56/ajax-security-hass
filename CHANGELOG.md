# Changelog

All notable changes to the Ajax Security System integration will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.4.8] - 2025-11-11

### Changed
- Added comprehensive debug logging for night mode detection (Issue #18)
  - Logs armed groups count vs total groups
  - Shows each group's state and night_mode_enabled flag
  - Displays which security state is being set and why
  - Helps diagnose alarm control panel state update issues

## [0.4.7] - 2025-11-11

### Added
- **Two-factor authentication (2FA/TOTP) support** (Fixes #17)
  - Automatic detection when 2FA is required
  - New TOTP step in configuration flow for entering 6-digit verification code
  - Support for authenticator apps (Google Authenticator, Authy, etc.)
  - Seamless experience: one step without 2FA, two steps with 2FA

### Fixed
- Fixed alarm control panel not updating when arming night mode in group mode (Fixes #18)
  - Properly detects night_mode_enabled flag on armed groups
  - Sets space security state to NIGHT_MODE when appropriate

### Technical
- Added `async_login_with_totp()` method to AjaxApi
- Added `Ajax2FARequiredError` exception with request_id
- Added `async_step_totp()` to config flow
- Updated translations (EN/FR) for 2FA step

## [0.4.6] - 2025-11-10

### Changed
- **Groups/zones now use switches instead of alarm control panels** (Fixes #2)
  - Groups/zones appear as switches: ON = Armed, OFF = Disarmed
  - Removed night mode from individual zones (not relevant)
  - Main alarm control panel remains for the entire space/hub
  - Simpler and more intuitive interface for zone control

### Fixed
- Fixed protobuf class names for group arming/disarming
  - `ArmGroupRequest` → `ArmSpaceGroupRequest`
  - `DisarmGroupRequest` → `DisarmSpaceGroupRequest`
- Resolved AttributeError when arming/disarming groups

### Technical
- Added `AjaxGroupSwitch` class to switch.py
- Removed `AjaxGroupAlarmControlPanel` class from alarm_control_panel.py
- Added translations for security_group switches (EN/FR)

## [0.4.5] - 2025-11-10

### Added
- **Support for Ajax wired input modules**
  - WireInput MT - Module for connecting third-party wired detectors (17 devices supported)
  - WireInput RS - Wired input module with relay functionality (6 devices supported)
  - LineSplit Fibra - Fibra line splitter/multiplexer (1 device supported)
  - These modules allow integration of existing wired security systems with Ajax

### Fixed
- Improved device type parsing to handle protobuf formatting artifacts
- Device types with formatting like `"wire_input_mt {\n}\n"` now correctly recognized

### Technical
- Added `WIRE_INPUT` and `LINE_SPLITTER` device types to DeviceType enum
- Enhanced `_parse_device_type()` to clean up raw type strings before mapping
- Added device type mappings for all wire input and line splitter variants

## [0.4.4] - 2025-11-10

### Added
- **Improved debug support for unknown device types**
  - Added `raw_type` field to AjaxDevice model to store original device type before parsing
  - Added `unknown_device_raw_types` summary in device info report
  - Warning logs when unknown device types are detected with their raw type
  - Enhanced notification to alert users when unknown devices are detected
  - Added `debug_unknown_devices.py` script for detailed device analysis

### Changed
- `ajax.generate_device_info` service now includes `raw_type` for each device to help identify unsupported device types

### Fixed
- Fixed dataclass field ordering issue that prevented integration from loading

## [0.4.3] - 2025-11-10

### Fixed
- **Group/zone alarm panels not being created on initial setup** (Fixes #6)
  - Fixed timing issue where group/zone alarm control panel entities were not created even though groups were detected
  - Added synchronization to wait for stream data containing groups before entity creation
  - Platform setup now waits up to 10 seconds for groups to be loaded from stream
  - Ensures group entities are created during initial setup without requiring additional API calls

### Technical
- Added `_groups_loaded_events` dictionary to track when groups are received from stream
- Added `_async_wait_for_groups()` method with 10-second timeout
- Enhanced `_async_handle_single_update()` to signal when security/group data arrives

## [0.4.2] - 2025-11-10

### Fixed
- **Duplicate malfunctions sensor**: Fixed unique ID conflict causing "ID already exists" errors
  - Removed redundant malfunctions sensor definition for hub devices
  - Now only one malfunctions sensor per device (problem binary sensor handles this)

### Documentation
- Updated README to clarify that 2FA is not yet supported
- Added note about authentication limitations

## [0.4.1] - 2025-11-10

### Fixed
- **TypeError in malfunctions sensor**: Fixed `object of type 'int' has no len()` error
  - Removed incorrect `len()` call on malfunctions count (already an integer)
  - Malfunctions sensor now correctly displays the count value

## [0.4.0] - 2025-11-10

### Fixed
- **Arm night state synchronization** (Fixes #16)
  - Removed all optimistic state updates that caused race conditions with real-time stream
  - Integration now relies solely on real-time stream for state updates
  - Fixed issue where alarm panel would get stuck in arming/pending state
  - All arm/disarm transitions now work correctly and synchronize with Ajax app

### Changed
- Alarm control panel entities no longer use optimistic updates
- State changes are only reflected after confirmation from Ajax cloud via stream

### Technical
- Removed `assumed_state` property from alarm control panel
- Removed optimistic state setting in arm/disarm methods
- Enhanced logging for arm/disarm operations

## [0.3.1] - 2025-11-10

### Added
- **Socket/Relay support**: Full control of Ajax smart sockets and relays
  - New switch platform for turning sockets on/off
  - Support for multi-channel devices (up to 4 channels)
  - Real-time state updates via coordinator
  - Channel information in entity attributes (channel_id, output_mode, operating_mode)
  - Added `async_turn_on_device` and `async_turn_off_device` API methods

### Fixed
- **Door sensor closed state detection**: Door sensors now properly detect both opening and closing
  - `door_opened` attribute now correctly initializes to `False` when absent
  - Previously only detected opening, not closing events
- **Users without notification access**: Integration no longer crashes for users without notification permissions
  - Returns empty notification list instead of raising exception
  - Logs informational message when access is denied
- **Real-time alarm status updates**: Improved reliability of streaming tasks
  - Added automatic retry logic with exponential backoff (5s, 10s, 20s, 40s, 60s intervals)
  - Streaming tasks now retry up to 10 times on errors
  - Enhanced logging to track streaming status and state changes
- **Duplicate hub devices**: Fixed issue where hub appeared twice in device list
  - Hub device-level sensors now merge with alarm control panel device
  - All hub entities (alarm panel, sensors, binary sensors) appear on single device
- **Privacy**: Removed email address from debug logs

### Technical
- Parse socket channel data from `spread_properties.channel`
- Device type filtering now uses explicit whitelisting for detector-specific sensors
- Alarm control panel now includes `device_info` property
- Hub device sensors use space identifier to merge with space-level entities

### Migration Notes
- **Manual cleanup required**: Users may need to manually remove the old duplicate hub device from Home Assistant after updating (Settings > Devices & Services > Ajax > find empty hub device and delete it)

## [0.3.0] - 2025-11-09

### Added
- **Group/Zone support**: Full support for Ajax systems using group mode
  - Groups/zones appear as separate alarm control panels
  - Each group can be armed/disarmed independently
  - Night mode support per group
  - Real-time state updates via gRPC streaming
- **Hub problem monitoring**: New "Problem" binary sensor for hubs
  - Shows when any device has malfunctions
  - Detailed attributes listing all devices with problems
  - Includes device name, type, room location, and malfunction count
- **Enhanced hub data parsing**:
  - Device firmware updates tracking (device_firmware_updates)
  - Hub connection properties (offline/online delay settings)
  - Installation and monitoring companies information

### Fixed
- **Binary sensor filtering**: "Always Active" and "Armed in Night Mode" sensors now only created for detector devices
  - Previously incorrectly appeared on repeaters/signal extenders
  - Now explicitly limited to: motion detectors, door contacts, glass break, smoke, flood, and temperature sensors
- **Firmware update detection**: Fixed false positive firmware update alerts
  - Corrected confusion between auto-update setting and actual available updates
  - Now properly distinguishes system_firmware_update (setting) from device_firmware_updates (actual updates)

### Changed
- **Malfunction sensor**: Now only created for hubs and enabled by default
  - Previously created for all devices but disabled by default

### Technical
- Added `_parse_groups_from_space()` method to parse group data from Space protobuf
- Added `_async_parse_groups_from_snapshot()` for coordinator group updates
- Enhanced group state handling in stream updates
- Added AjaxGroup model with GroupState enum
- Device type whitelisting for detector-specific binary sensors
- Comprehensive logging for hub device data and firmware updates

## [0.2.1] - 2025-11-09

### Added
- **Binary sensors for device arming settings**:
  - "Always Active" sensor - shows if device is active even when system is disarmed
  - "Armed in Night Mode" sensor - shows if device is armed during night mode
  - These sensors are only created for detectors (motion, door, etc.), not for hubs

### Fixed
- **Keyboard/keypad device recognition**: All Ajax keyboard variants now properly recognized
  - Added support for: Keyboard, KeypadPlus, KeypadSPlus, KeypadPlusG3
  - Added support for: KeypadCombi, KeyboardFibra, KeypadTouchscreen
  - Added support for: KeypadBeep, KeypadBase, KeypadOutdoor variants
  - Previously these devices were showing as "unknown" type
- **Hub binary sensors**: Removed incorrect "Always Active" and "Armed in Night Mode" sensors from hub devices (these settings only apply to detectors)

### Technical
- Added parsing for `always_active` and `armed_in_night_mode` status fields from Ajax API
- Enhanced device type mapping with 30+ keyboard/keypad variant names
- Added debug logging for `spread_properties` and `device_specific_properties`

## [0.2.0] - 2025-11-09

### Added
- **Real-time notification streaming**: Notifications now arrive instantly via gRPC streaming, eliminating the 30-second polling delay
- **Binary Sensor platform**: New comprehensive device monitoring
  - Motion sensors with automatic 30-second reset
  - Door/Window contact sensors
  - Smoke detectors
  - Leak/Water detectors
  - Tamper sensors (disabled by default)
- **Last Alert sensor**: Shows most recent security event with timestamp, device name, room location, and event type

### Changed
- **IoT class**: Updated from `cloud_polling` to `cloud_push` for real-time capabilities
- **Timezone handling**: Fixed datetime timezone issues causing Home Assistant warnings
- **French translations**: Complete localization for all new sensors and attributes

### Technical
- Added `async_stream_notification_updates()` method for real-time notification streaming
- Implemented background task management for notification streams
- Enhanced coordinator to process notification events as they arrive
- Added `_async_process_notification_event()` for instant device state updates
- Proper cleanup of streaming tasks on shutdown

## [0.1.2] - 2025-11-09

### Fixed
- **Critical**: Fixed grpcio dependency conflict with Home Assistant
  - Changed requirement from `grpcio==1.72.1` to `grpcio>=1.62.0`
  - Now compatible with both Home Assistant 2024.10.x (grpcio 1.72.1) and 2024.11+ (grpcio 1.75.1)
  - Resolves issue #1 where integration failed to load due to dependency conflicts

### Changed
- Updated manifest.json to use flexible grpcio version constraint

## [0.1.1] - 2025-11-09

### Fixed
- Fixed grpcio version compatibility for Home Assistant OS 2025.10.4
  - Updated all v3 protobuf files to use grpcio 1.72.1 instead of 1.75.1
  - Changed manifest requirement from `grpcio>=1.60.0` to `grpcio==1.72.1`

### Note
- This version was superseded by 0.1.2 due to dependency conflicts with newer Home Assistant versions

## [0.1.0] - 2025-11-08

### Added
- **Initial release** of Ajax Security System integration
- **Alarm Control Panel** platform
  - Real-time security mode control (Armed, Disarmed, Night Mode)
  - Live status updates via gRPC streaming
  - Instant synchronization with Ajax mobile app
- **Sensor** platform
  - Battery level monitoring for all devices
  - Temperature readings from supported devices
  - Hub status monitoring
- **Button** platform
  - Panic button activation
  - Siren test functionality
- **HACS Support**
  - Added hacs.json configuration
  - Added info.md documentation for HACS store
- **GitHub Actions**
  - Automatic release creation on version tags
  - ZIP package generation for easy installation

### Technical Details
- Uses gRPC streaming for real-time updates
- 60-second polling interval for minimal API load
- Direct communication using Ajax's mobile app protocol
- Compatible with Home Assistant 2023.8.0+

### Known Limitations
- Binary sensors not yet implemented (motion, door/window, smoke detectors)
- Only tested with Ajax Hub and basic sensors
- Requires connection to Ajax cloud services (no offline mode)

---

## Release Notes Format

For each release, we document:
- **Added**: New features
- **Changed**: Changes in existing functionality
- **Deprecated**: Soon-to-be removed features
- **Removed**: Removed features
- **Fixed**: Bug fixes
- **Security**: Security vulnerability fixes

## Contributing

When contributing changes, please update this CHANGELOG.md file with your changes under the "Unreleased" section at the top of the file.
