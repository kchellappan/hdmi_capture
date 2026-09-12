// SPDX-License-Identifier: MIT
#include "vcap/device.hpp"

#include <linux/videodev2.h>
#include <sys/ioctl.h>
#include <sys/stat.h>
#include <dirent.h>
#include <fcntl.h>
#include <unistd.h>
#include <limits.h>

#include <algorithm>
#include <cerrno>
#include <cstring>
#include <fstream>
#include <stdexcept>

namespace vcap {
namespace {

// Unlike the Python client, which hand-rolls every structure and ioctl number in ctypes
// and pins them with a test, this side just includes <linux/videodev2.h>. The ABI comes
// from the kernel's own header, so there is nothing to get wrong and nothing to pin.
std::string read_file(const std::string& path) {
    std::ifstream in(path);
    if (!in) return {};
    std::string s;
    std::getline(in, s);
    while (!s.empty() && (s.back() == '\n' || s.back() == ' ')) s.pop_back();
    return s;
}

std::string real_path(const std::string& p) {
    char buf[PATH_MAX];
    if (::realpath(p.c_str(), buf)) return std::string(buf);
    return p;
}

std::string basename_of(const std::string& p) {
    const auto slash = p.find_last_of('/');
    return slash == std::string::npos ? p : p.substr(slash + 1);
}

std::vector<std::string> list_dir(const std::string& dir) {
    std::vector<std::string> out;
    DIR* d = ::opendir(dir.c_str());
    if (!d) return out;
    while (dirent* e = ::readdir(d)) {
        const std::string name = e->d_name;
        if (name == "." || name == "..") continue;
        out.push_back(dir + "/" + name);
    }
    ::closedir(d);
    std::sort(out.begin(), out.end());
    return out;
}

bool lower_contains(const std::string& haystack, const std::string& needle) {
    std::string h = haystack, n = needle;
    std::transform(h.begin(), h.end(), h.begin(), ::tolower);
    std::transform(n.begin(), n.end(), n.begin(), ::tolower);
    return h.find(n) != std::string::npos;
}

}  // namespace

bool Device::is_capture() const  { return (device_caps & V4L2_CAP_VIDEO_CAPTURE) != 0; }
bool Device::is_metadata() const { return (device_caps & V4L2_CAP_META_CAPTURE) != 0; }

UsbInfo Device::usb() const {
    UsbInfo info;
    // Walk up from the video node's own sysfs entry rather than parsing bus_info.
    // bus_info names the PCI host controller and port chain ("usb-0000:00:14.0-2"),
    // which is not the bus-port form sysfs uses ("4-2"), so matching them textually
    // means reconstructing the kernel's bus numbering. The symlink already knows.
    std::string cur = real_path("/sys/class/video4linux/" + basename_of(node) + "/device");
    for (int hop = 0; hop < 4; ++hop) {
        struct stat st;
        if (::stat((cur + "/idVendor").c_str(), &st) == 0) {
            info.present = true;
            info.vendor_id  = read_file(cur + "/idVendor");
            info.product_id = read_file(cur + "/idProduct");
            info.vendor     = read_file(cur + "/manufacturer");
            info.product    = read_file(cur + "/product");
            info.serial     = read_file(cur + "/serial");
            info.speed_mbps = read_file(cur + "/speed");
            return info;
        }
        const auto slash = cur.find_last_of('/');
        if (slash == std::string::npos || slash == 0) break;
        cur = cur.substr(0, slash);
    }
    return info;
}

Device open_device(const std::string& path) {
    Device dev;
    dev.path = path;
    dev.node = real_path(path);

    const int fd = ::open(dev.node.c_str(), O_RDWR | O_NONBLOCK);
    if (fd < 0) {
        throw std::runtime_error("cannot open " + dev.node + ": " + std::strerror(errno));
    }
    v4l2_capability cap{};
    if (::ioctl(fd, VIDIOC_QUERYCAP, &cap) < 0) {
        const int err = errno;
        ::close(fd);
        throw std::runtime_error("VIDIOC_QUERYCAP on " + dev.node + ": " +
                                 std::strerror(err));
    }
    ::close(fd);

    dev.driver      = reinterpret_cast<const char*>(cap.driver);
    dev.card        = reinterpret_cast<const char*>(cap.card);
    dev.bus_info    = reinterpret_cast<const char*>(cap.bus_info);
    dev.device_caps = cap.device_caps;
    return dev;
}

std::vector<Device> enumerate_devices() {
    // Map each real node to its by-id link, so a device that has one is reported by the
    // name that survives a replug.
    std::vector<std::pair<std::string, std::string>> by_node;
    for (const auto& link : list_dir("/dev/v4l/by-id")) {
        by_node.emplace_back(real_path(link), link);
    }

    std::vector<Device> out;
    for (const auto& entry : list_dir("/dev")) {
        if (basename_of(entry).rfind("video", 0) != 0) continue;
        std::string path = entry;
        for (const auto& [node, link] : by_node) {
            if (node == entry) { path = link; break; }
        }
        try {
            Device dev = open_device(path);
            if (dev.is_capture()) out.push_back(dev);
        } catch (const std::runtime_error&) {
            continue;  // busy, or no permission; not ours to report on
        }
    }
    std::sort(out.begin(), out.end(),
              [](const Device& a, const Device& b) { return a.node < b.node; });
    return out;
}

Device find_capture_card(const std::string& hint) {
    struct stat st;
    if (!hint.empty() && ::stat(hint.c_str(), &st) == 0) return open_device(hint);

    const auto candidates = enumerate_devices();
    std::string found;
    for (const auto& d : candidates) {
        if (!found.empty()) found += ", ";
        found += d.path + " (" + d.card + ")";
    }

    if (!hint.empty()) {
        std::vector<Device> matched;
        for (const auto& d : candidates) {
            if (lower_contains(d.path, hint) || lower_contains(d.card, hint)) {
                matched.push_back(d);
            }
        }
        if (matched.empty()) {
            throw std::runtime_error("no capture device matching '" + hint + "'; found " +
                                     (found.empty() ? "nothing" : found));
        }
        if (matched.size() > 1) {
            std::string paths;
            for (const auto& d : matched) {
                if (!paths.empty()) paths += ", ";
                paths += d.path;
            }
            throw std::runtime_error("'" + hint + "' matches more than one device: " + paths);
        }
        return matched.front();
    }

    if (candidates.empty()) {
        throw std::runtime_error(
            "no V4L2 capture device found. If the card is plugged in, check that this "
            "user can open /dev/video* -- see scripts/install_deps.sh");
    }
    if (candidates.size() > 1) {
        throw std::runtime_error("more than one capture device; pass one explicitly. "
                                 "Found: " + found);
    }
    return candidates.front();
}

}  // namespace vcap
