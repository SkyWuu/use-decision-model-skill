#!/usr/bin/env bash
# Fixture: cpp-service — a C++ intent-routing service for e-commerce customer chat.
#
# Planted for the grader (see evals/cases.yaml):
#   substitution  src/intent_router.cc   Chinese prompt in a raw string, internal LLM over libcurl,
#                                        label parsed with std::string::find in a fixed order
#   substitution  src/keyword_filter.cc  refund fast path by std::string::find over a keyword list, 3 fix commits
#   not a fit     src/quota.cc           per-user daily quota arithmetic
#   volume        conf/service.conf      max_qps = 50 (peak only; daily volume is not stated)
# Usage: build.sh <out-dir>
set -euo pipefail
. "$(cd "$(dirname "$0")/.." && pwd)/_lib.sh"
fixture_init "$@"

MA="Ma Jun <majun@example.com>"
GAO="Gao Ning <gaoning@example.com>"

mkdir -p src conf test

# ---------------------------------------------------------------- 1. skeleton
cat > README.md <<'EOF'
# intent-router

电商客服对话的意图路由服务（C++17）。用户在 App 里发消息，本服务判断意图，把会话转给对应的处理流程：订单查询机器人、改地址机器人、退款流程、投诉专席或人工客服。

- 配置：`conf/service.conf`（峰值限流、LLM 超时）。
- LLM：通过内部 LLM 网关的 HTTP 接口调用，见 `src/llm_client.cc`。
- 构建：`cmake -B build && cmake --build build`。
EOF

cat > CMakeLists.txt <<'EOF'
cmake_minimum_required(VERSION 3.20)
project(intent_router CXX)
set(CMAKE_CXX_STANDARD 17)
find_package(CURL REQUIRED)
add_executable(intent_router
  src/main.cc src/llm_client.cc src/intent_router.cc src/keyword_filter.cc src/quota.cc)
target_link_libraries(intent_router CURL::libcurl)
EOF

cat > conf/service.conf <<'EOF'
# 意图路由服务配置
listen = 0.0.0.0:9300
max_qps = 50                 # 峰值限流
llm_gateway = http://llm-gateway.internal.example/v1/chat/completions
llm_timeout_ms = 3000
daily_llm_quota_per_user = 200
EOF

cat > src/llm_client.h <<'EOF'
#pragma once
#include <string>

namespace cs {

// LlmClient 调用内部 LLM 网关，返回模型生成的文本。
class LlmClient {
 public:
  LlmClient(std::string endpoint, int timeout_ms);
  std::string Complete(const std::string& prompt);

 private:
  std::string endpoint_;
  int timeout_ms_;
};

}  // namespace cs
EOF

cat > src/llm_client.cc <<'EOF'
#include "llm_client.h"

#include <curl/curl.h>

#include <string>

namespace cs {

namespace {
size_t WriteBody(char* ptr, size_t size, size_t nmemb, void* userdata) {
  static_cast<std::string*>(userdata)->append(ptr, size * nmemb);
  return size * nmemb;
}

std::string EscapeJson(const std::string& s) {
  std::string out;
  for (char c : s) {
    if (c == '"' || c == '\\') out.push_back('\\');
    if (c == '\n') { out += "\\n"; continue; }
    out.push_back(c);
  }
  return out;
}

// 只取 choices[0].message.content，简单起见不引入 JSON 库
std::string ExtractContent(const std::string& body) {
  const std::string key = "\"content\":\"";
  auto start = body.find(key);
  if (start == std::string::npos) return "";
  start += key.size();
  auto end = body.find('"', start);
  return body.substr(start, end - start);
}
}  // namespace

LlmClient::LlmClient(std::string endpoint, int timeout_ms)
    : endpoint_(std::move(endpoint)), timeout_ms_(timeout_ms) {}

std::string LlmClient::Complete(const std::string& prompt) {
  CURL* curl = curl_easy_init();
  std::string body;
  std::string payload = "{\"model\":\"general-medium\",\"temperature\":0,\"messages\":[{\"role\":\"user\",\"content\":\"" +
                        EscapeJson(prompt) + "\"}]}";
  struct curl_slist* headers = curl_slist_append(nullptr, "Content-Type: application/json");
  curl_easy_setopt(curl, CURLOPT_URL, endpoint_.c_str());
  curl_easy_setopt(curl, CURLOPT_HTTPHEADER, headers);
  curl_easy_setopt(curl, CURLOPT_POSTFIELDS, payload.c_str());
  curl_easy_setopt(curl, CURLOPT_TIMEOUT_MS, timeout_ms_);
  curl_easy_setopt(curl, CURLOPT_WRITEFUNCTION, WriteBody);
  curl_easy_setopt(curl, CURLOPT_WRITEDATA, &body);
  CURLcode rc = curl_easy_perform(curl);
  curl_slist_free_all(headers);
  curl_easy_cleanup(curl);
  if (rc != CURLE_OK) return "";
  return ExtractContent(body);
}

}  // namespace cs
EOF

cat > src/intent_router.h <<'EOF'
#pragma once
#include <string>

#include "llm_client.h"

namespace cs {

enum class Intent { kQueryOrder, kChangeAddress, kRefund, kComplaint, kHuman, kOther };

Intent RouteIntent(const std::string& query, LlmClient& llm);

}  // namespace cs
EOF

cat > src/intent_router.cc <<'EOF'
#include "intent_router.h"

#include <string>

namespace cs {

static const char* kIntentPrompt = R"(你是电商客服的意图识别助手。
请判断用户这句话的意图，只能回答以下之一：查询订单、修改地址、退款退货、投诉、转人工、其他。
不要解释，只输出意图名称。
用户输入：)";

Intent RouteIntent(const std::string& query, LlmClient& llm) {
  std::string answer = llm.Complete(std::string(kIntentPrompt) + query);
  if (answer.find("查询订单") != std::string::npos) return Intent::kQueryOrder;
  if (answer.find("修改地址") != std::string::npos) return Intent::kChangeAddress;
  if (answer.find("退款") != std::string::npos) return Intent::kRefund;
  if (answer.find("投诉") != std::string::npos) return Intent::kComplaint;
  if (answer.find("人工") != std::string::npos) return Intent::kHuman;
  return Intent::kOther;  // 超时、空回复或无法识别都算其他
}

}  // namespace cs
EOF

cat > src/main.cc <<'EOF'
#include <iostream>
#include <string>

#include "intent_router.h"
#include "llm_client.h"

// 简化的请求处理：从标准输入逐行读用户消息，输出意图编号。
int main() {
  cs::LlmClient llm("http://llm-gateway.internal.example/v1/chat/completions", 3000);
  std::string line;
  while (std::getline(std::cin, line)) {
    cs::Intent intent = cs::RouteIntent(line, llm);
    std::cout << static_cast<int>(intent) << std::endl;
  }
  return 0;
}
EOF
commit "$MA" "2026-04-01T10:30:00+08:00" "init: 意图路由服务，LLM 识别用户意图"

# ---------------------------------------------------------------- 2. keyword fast path + quota
cat > src/keyword_filter.h <<'EOF'
#pragma once
#include <string>

namespace cs {

// LooksLikeRefund 命中退款关键词时直接进入退款流程，不再调用 LLM。
bool LooksLikeRefund(const std::string& text);

}  // namespace cs
EOF

cat > src/keyword_filter.cc <<'EOF'
#include "keyword_filter.h"

#include <string>
#include <vector>

namespace cs {

static const std::vector<std::string> kRefundWords = {"退款", "退钱", "退货", "不要了"};

bool LooksLikeRefund(const std::string& text) {
  for (const auto& w : kRefundWords) {
    if (text.find(w) != std::string::npos) return true;
  }
  return false;
}

}  // namespace cs
EOF

cat > src/quota.h <<'EOF'
#pragma once
#include <cstdint>
#include <string>
#include <unordered_map>

namespace cs {

// Quota 按用户统计当天的 LLM 调用次数，超过上限就不再调用 LLM，直接转人工。
class Quota {
 public:
  explicit Quota(int daily_limit) : daily_limit_(daily_limit) {}
  bool TryConsume(const std::string& user_id, int64_t day);

 private:
  int daily_limit_;
  std::unordered_map<std::string, std::pair<int64_t, int>> used_;
};

}  // namespace cs
EOF

cat > src/quota.cc <<'EOF'
#include "quota.h"

namespace cs {

bool Quota::TryConsume(const std::string& user_id, int64_t day) {
  auto& entry = used_[user_id];
  if (entry.first != day) {
    entry = {day, 0};
  }
  if (entry.second >= daily_limit_) return false;
  ++entry.second;
  return true;
}

}  // namespace cs
EOF

cat > src/main.cc <<'EOF'
#include <ctime>
#include <iostream>
#include <string>

#include "intent_router.h"
#include "keyword_filter.h"
#include "llm_client.h"
#include "quota.h"

// 简化的请求处理：每行输入为 "<user_id>\t<消息>"，输出意图编号。
int main() {
  cs::LlmClient llm("http://llm-gateway.internal.example/v1/chat/completions", 3000);
  cs::Quota quota(200);
  std::string line;
  while (std::getline(std::cin, line)) {
    auto tab = line.find('\t');
    std::string user = line.substr(0, tab);
    std::string text = line.substr(tab + 1);
    int64_t day = static_cast<int64_t>(std::time(nullptr)) / 86400;

    cs::Intent intent;
    if (cs::LooksLikeRefund(text)) {
      intent = cs::Intent::kRefund;  // 快速通道：不调用 LLM
    } else if (!quota.TryConsume(user, day)) {
      intent = cs::Intent::kHuman;
    } else {
      intent = cs::RouteIntent(text, llm);
    }
    std::cout << static_cast<int>(intent) << std::endl;
  }
  return 0;
}
EOF
commit "$GAO" "2026-04-22T15:00:00+08:00" "feat: 退款关键词快速通道和用户级 LLM 配额"

# ---------------------------------------------------------------- 3. keyword fixes
cat > src/keyword_filter.cc <<'EOF'
#include "keyword_filter.h"

#include <string>
#include <vector>

namespace cs {

static const std::vector<std::string> kRefundWords = {"退款", "退钱", "退货", "不要了"};
static const std::vector<std::string> kExclusions = {"不要了解", "不要了吧"};

bool LooksLikeRefund(const std::string& text) {
  for (const auto& e : kExclusions) {
    if (text.find(e) != std::string::npos) return false;
  }
  for (const auto& w : kRefundWords) {
    if (text.find(w) != std::string::npos) return true;
  }
  return false;
}

}  // namespace cs
EOF
commit "$GAO" "2026-05-13T11:20:00+08:00" "fix(keyword): 误判——“我不要了解这些”被当成退款"

cat > src/keyword_filter.cc <<'EOF'
#include "keyword_filter.h"

#include <string>
#include <vector>

namespace cs {

static const std::vector<std::string> kRefundWords = {"退款", "退钱", "退货", "不要了", "钱退", "退回来"};
static const std::vector<std::string> kExclusions = {"不要了解", "不要了吧"};

bool LooksLikeRefund(const std::string& text) {
  for (const auto& e : kExclusions) {
    if (text.find(e) != std::string::npos) return false;
  }
  for (const auto& w : kRefundWords) {
    if (text.find(w) != std::string::npos) return true;
  }
  return false;
}

}  // namespace cs
EOF
commit "$MA" "2026-06-09T09:45:00+08:00" "fix(keyword): 漏判“把钱退给我”“东西寄回去钱退回来”"

cat > src/keyword_filter.cc <<'EOF'
#include "keyword_filter.h"

#include <string>
#include <vector>

namespace cs {

static const std::vector<std::string> kRefundWords = {"退款", "退钱", "退货", "不要了", "钱退", "退回来"};
// 命中这些说法时不走退款快速通道
static const std::vector<std::string> kExclusions = {"不要了解", "不要了吧", "退货地址", "退款进度", "退款到账"};

bool LooksLikeRefund(const std::string& text) {
  for (const auto& e : kExclusions) {
    if (text.find(e) != std::string::npos) return false;
  }
  for (const auto& w : kRefundWords) {
    if (text.find(w) != std::string::npos) return true;
  }
  return false;
}

}  // namespace cs
EOF

cat > test/keyword_filter_test.cc <<'EOF'
#include <cassert>
#include <string>

#include "../src/keyword_filter.h"

int main() {
  using cs::LooksLikeRefund;
  assert(LooksLikeRefund("我要退款"));
  assert(LooksLikeRefund("这个不要了，怎么退"));
  assert(LooksLikeRefund("把钱退给我"));
  assert(!LooksLikeRefund("我不要了解这些套餐"));
  assert(!LooksLikeRefund("退货地址在哪里"));   // 是咨询，不是申请退款
  assert(!LooksLikeRefund("退款进度怎么查"));   // 应该走查询订单
  // 已知问题：“买的鞋子小了想换个码”应该是换货，目前会落到 LLM 再被识别成退款
  return 0;
}
EOF
commit "$GAO" "2026-07-21T16:30:00+08:00" "fix(keyword): “退货地址在哪”“退款进度”被当成退款申请，进了退款流程" "这类咨询应该走查询订单机器人。"

# ---------------------------------------------------------------- 4. prompt tweak
cat > src/intent_router.cc <<'EOF'
#include "intent_router.h"

#include <string>

namespace cs {

static const char* kIntentPrompt = R"(你是电商客服的意图识别助手。
请判断用户这句话的意图，只能回答以下之一：查询订单、修改地址、退款退货、投诉、转人工、其他。
如果用户只是询问退款进度，回答“查询订单”。
不要解释，只输出意图名称。
用户输入：)";

Intent RouteIntent(const std::string& query, LlmClient& llm) {
  std::string answer = llm.Complete(std::string(kIntentPrompt) + query);
  if (answer.find("查询订单") != std::string::npos) return Intent::kQueryOrder;
  if (answer.find("修改地址") != std::string::npos) return Intent::kChangeAddress;
  if (answer.find("退款") != std::string::npos) return Intent::kRefund;
  if (answer.find("投诉") != std::string::npos) return Intent::kComplaint;
  if (answer.find("人工") != std::string::npos) return Intent::kHuman;
  return Intent::kOther;  // 超时、空回复或无法识别都算其他
}

}  // namespace cs
EOF
commit "$MA" "2026-08-26T10:15:00+08:00" "fix(intent): 询问退款进度被识别成退款退货，prompt 里补充说明"
