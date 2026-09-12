// SPDX-License-Identifier: MIT
//
// Finding the capture device, and identifying it in terms that outlive a replug.
//
// Device node numbering is not stable: a UVC device registers both a video node and a
// metadata node, and which /dev/videoN each lands on depends on what enumerated first.
// Everything here resolves through /dev/v4l/by-id, and a recording records both the
// stable path and the node it happened to be at capture time.
#pragma once

#include <cstdint>
#include <string>
#include <vector>

namespace vcap {

struct UsbInfo {
    std::string vendor_id, product_id, vendor, product, serial, speed_mbps;
    bool present = false;
};

struct Device {
    std::string path;      // the by-id symlink where one exists, else /dev/videoN
    std::string node;      // what it resolves to right now
    std::string driver;
    std::string card;
    std::string bus_info;
    std::uint32_t device_caps = 0;

    bool is_capture() const;
    bool is_metadata() const;
    UsbInfo usb() const;
};

// Query a node without starting a stream. Throws std::runtime_error if it cannot be
// opened or does not answer VIDIOC_QUERYCAP.
Device open_device(const std::string& path);

// Every capture node on the machine, preferring by-id paths. Metadata nodes are excluded:
// they share a card name with the video node they belong to and would otherwise appear as
// a second, unusable capture device.
std::vector<Device> enumerate_devices();

// The capture device to use. `hint` is matched as a substring against the by-id path and
// the card name, which is what makes a config portable -- "MACROSILICON" identifies the
// card regardless of which /dev/videoN it landed on this boot. An empty hint requires
// exactly one candidate. Throws std::runtime_error with a message naming what was found
// instead.
Device find_capture_card(const std::string& hint = "");

}  // namespace vcap
