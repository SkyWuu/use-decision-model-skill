#!/usr/bin/env bash
# Fixture: content-moderation-go — a community content platform backend in Go.
#
# Planted for the grader (see evals/cases.yaml):
#   substitution  internal/moderation/banned.go  spam/lead-generation regexes with 3 fix commits;
#                                                ADR 0002 says the regulated-word list must stay deterministic
#   substitution  internal/dedup/dedup.go        cosine > threshold, threshold churned 0.90 -> 0.85 -> 0.88 -> 0.85
#   augmentation  internal/feed/rank.go          recall 50, keep [:20], sort by time, same for everyone
#   augmentation  internal/review/sampler.go     5% random sampling of auto-approved comments
#   not a fit     internal/billing/price.go      promotion pricing arithmetic
#   not a fit     internal/auth/permission.go    role checks
#   volume        config/jobs.yaml               feed job every 10 min x 500 users; comment volume is unstated
# Usage: build.sh <out-dir>
set -euo pipefail
. "$(cd "$(dirname "$0")/.." && pwd)/_lib.sh"
fixture_init "$@"

WANG="Wang Lei <wanglei@example.com>"
ZHAO="Zhao Yu <zhaoyu@example.com>"
SUN="Sun Qi <sunqi@example.com>"

mkdir -p cmd/server config docs/adr internal/{moderation,dedup,feed,review,billing,auth,embedding,model}

# ---------------------------------------------------------------- 1. skeleton
cat > README.md <<'EOF'
# community-backend

社区内容平台后端：评论审核、帖子去重、首页信息流、人工抽检和推广计费。

## 模块

- `internal/moderation`：评论机审。命中监管违禁词或引流广告规则就拦截。
- `internal/dedup`：新帖与近期帖子去重，重复的帖子合并到原帖。
- `internal/feed`：首页信息流排序，由定时任务批量重算，见 `config/jobs.yaml`。
- `internal/review`：已通过机审的评论按比例抽检，交给审核员。
- `internal/billing`：推广帖计费。
- `internal/auth`：角色与权限。

## 约定

- 监管违禁词必须用确定性规则拦截，见 `docs/adr/0002-regulated-words.md`。
EOF

cat > go.mod <<'EOF'
module example.com/community

go 1.23
EOF

cat > internal/model/model.go <<'EOF'
package model

import "time"

type Comment struct {
	ID       string
	PostID   string
	AuthorID string
	Text     string
	Created  time.Time
}

type Post struct {
	ID        string
	AuthorID  string
	Title     string
	Body      string
	Topic     string
	Heat      float64
	CreatedAt time.Time
}

type User struct {
	ID        string
	Following []string
	Role      string
}
EOF

cat > internal/embedding/client.go <<'EOF'
package embedding

// Client 调用内部向量服务，返回文本的向量表示。
type Client interface {
	Embed(text string) []float32
}
EOF

cat > internal/moderation/regulated_words.txt <<'EOF'
# 监管通报的违禁词，由合规组维护，每行一个。示例数据。
示例违禁词甲
示例违禁词乙
示例违禁词丙
EOF

cat > internal/moderation/banned.go <<'EOF'
package moderation

import (
	_ "embed"
	"regexp"
	"strings"
)

//go:embed regulated_words.txt
var regulatedWordsFile string

// regulatedWords 是监管违禁词，按 ADR 0002 必须精确匹配拦截。
var regulatedWords = loadWords(regulatedWordsFile)

// spamPatterns 识别引流、广告类评论。
var spamPatterns = []*regexp.Regexp{
	regexp.MustCompile(`加微信`),
	regexp.MustCompile(`(?i)v[x信]\s*[:：]?\s*[a-z0-9_-]{5,}`),
	regexp.MustCompile(`刷单`),
	regexp.MustCompile(`代开发票`),
}

func ContainsRegulatedWord(text string) bool {
	for _, w := range regulatedWords {
		if strings.Contains(text, w) {
			return true
		}
	}
	return false
}

func LooksLikeSpam(text string) bool {
	for _, p := range spamPatterns {
		if p.MatchString(text) {
			return true
		}
	}
	return false
}

func loadWords(file string) []string {
	var words []string
	for _, line := range strings.Split(file, "\n") {
		line = strings.TrimSpace(line)
		if line == "" || strings.HasPrefix(line, "#") {
			continue
		}
		words = append(words, line)
	}
	return words
}
EOF

cat > internal/moderation/moderate.go <<'EOF'
package moderation

import "example.com/community/internal/model"

type Verdict int

const (
	Publish Verdict = iota
	Block
)

// Moderate 对新评论做机审。
func Moderate(c model.Comment) Verdict {
	if ContainsRegulatedWord(c.Text) {
		return Block
	}
	if LooksLikeSpam(c.Text) {
		return Block
	}
	return Publish
}
EOF

cat > docs/adr/0002-regulated-words.md <<'EOF'
# ADR 0002：监管违禁词必须用确定性规则拦截

- 状态：已接受
- 日期：2026-03-05

## 背景

监管部门会不定期通报违禁词，要求平台“通报即拦截”，并能对每次拦截给出命中的词。

## 决定

`internal/moderation/regulated_words.txt` 里的词一律精确匹配拦截，不经过任何模型，也不设置例外。名单由合规组维护。

## 不在本决定范围内

引流、广告、刷单等“像不像垃圾评论”的判断不是监管硬性要求，目前用 `spamPatterns` 正则做，可以替换成更好的方案。
EOF

cat > cmd/server/main.go <<'EOF'
package main

import (
	"encoding/json"
	"log"
	"net/http"

	"example.com/community/internal/model"
	"example.com/community/internal/moderation"
)

func main() {
	http.HandleFunc("/api/comments", func(w http.ResponseWriter, r *http.Request) {
		var c model.Comment
		if err := json.NewDecoder(r.Body).Decode(&c); err != nil {
			http.Error(w, "bad request", http.StatusBadRequest)
			return
		}
		if moderation.Moderate(c) == moderation.Block {
			http.Error(w, "评论包含违规内容", http.StatusForbidden)
			return
		}
		w.WriteHeader(http.StatusCreated)
	})
	log.Fatal(http.ListenAndServe(":8080", nil))
}
EOF
commit "$WANG" "2026-03-05T10:00:00+08:00" "init: 评论机审、监管违禁词和引流规则"

# ---------------------------------------------------------------- 2. spam fix 1
cat > internal/moderation/banned.go <<'EOF'
package moderation

import (
	_ "embed"
	"regexp"
	"strings"
)

//go:embed regulated_words.txt
var regulatedWordsFile string

// regulatedWords 是监管违禁词，按 ADR 0002 必须精确匹配拦截。
var regulatedWords = loadWords(regulatedWordsFile)

// spamPatterns 识别引流、广告类评论。
var spamPatterns = []*regexp.Regexp{
	regexp.MustCompile(`加微信[^支付]`), // “加微信支付优惠”是正常评论
	regexp.MustCompile(`(?i)v[x信]\s*[:：]?\s*[a-z0-9_-]{5,}`),
	regexp.MustCompile(`刷单`),
	regexp.MustCompile(`代开发票`),
}

func ContainsRegulatedWord(text string) bool {
	for _, w := range regulatedWords {
		if strings.Contains(text, w) {
			return true
		}
	}
	return false
}

func LooksLikeSpam(text string) bool {
	for _, p := range spamPatterns {
		if p.MatchString(text) {
			return true
		}
	}
	return false
}

func loadWords(file string) []string {
	var words []string
	for _, line := range strings.Split(file, "\n") {
		line = strings.TrimSpace(line)
		if line == "" || strings.HasPrefix(line, "#") {
			continue
		}
		words = append(words, line)
	}
	return words
}
EOF
commit "$ZHAO" "2026-03-26T16:20:00+08:00" "fix(moderation): 误杀“加微信支付有优惠”这类正常评论"

# ---------------------------------------------------------------- 3. dedup
cat > internal/dedup/dedup.go <<'EOF'
package dedup

import (
	"math"

	"example.com/community/internal/embedding"
	"example.com/community/internal/model"
)

// duplicateThreshold 以上视为重复帖，合并到原帖。
const duplicateThreshold = 0.90

// IsDuplicate 判断新帖是否与已有帖子重复。
func IsDuplicate(newPost, existing model.Post, emb embedding.Client) bool {
	a := emb.Embed(newPost.Title + "\n" + newPost.Body)
	b := emb.Embed(existing.Title + "\n" + existing.Body)
	return cosine(a, b) > duplicateThreshold
}

func cosine(a, b []float32) float64 {
	var dot, na, nb float64
	for i := range a {
		dot += float64(a[i] * b[i])
		na += float64(a[i] * a[i])
		nb += float64(b[i] * b[i])
	}
	return dot / (math.Sqrt(na) * math.Sqrt(nb))
}
EOF
commit "$SUN" "2026-04-02T11:10:00+08:00" "feat(dedup): 新帖与近期帖子按向量相似度去重"

cat > config/jobs.yaml <<'EOF'
jobs:
  feed_rank:
    schedule: "*/10 * * * *"   # 每 10 分钟一批
    batch_size: 500             # 每批重算 500 个活跃用户的首页信息流
  dedup_scan:
    schedule: "0 * * * *"      # 每小时一次
    batch_size: 2000            # 扫描最近 2000 篇新帖，每篇与同话题近 7 天的帖子比较
EOF
sed -i.bak 's/duplicateThreshold = 0.90/duplicateThreshold = 0.85/' internal/dedup/dedup.go && rm internal/dedup/dedup.go.bak
commit "$SUN" "2026-04-20T15:45:00+08:00" "fix(dedup): 改了几个字的搬运帖没被识别，阈值从 0.90 降到 0.85"

# ---------------------------------------------------------------- 4. feed ranking
cat > internal/feed/rank.go <<'EOF'
package feed

import (
	"sort"

	"example.com/community/internal/model"
)

// Store 提供召回所需的数据。
type Store interface {
	PostsByAuthors(authorIDs []string, limit int) []model.Post
	HotPosts(limit int) []model.Post
}

// RankFeed 为用户生成首页信息流。
func RankFeed(user model.User, store Store) []model.Post {
	candidates := recall(user, store, 50) // 按关注和热度召回 50 条
	if len(candidates) > 20 {
		candidates = candidates[:20] // 只保留召回分最高的 20 条
	}
	sort.Slice(candidates, func(i, j int) bool {
		return candidates[i].CreatedAt.After(candidates[j].CreatedAt)
	})
	return candidates
}

func recall(user model.User, store Store, n int) []model.Post {
	posts := store.PostsByAuthors(user.Following, n)
	if len(posts) < n {
		posts = append(posts, store.HotPosts(n-len(posts))...)
	}
	sort.Slice(posts, func(i, j int) bool { return posts[i].Heat > posts[j].Heat })
	return posts
}
EOF
commit "$WANG" "2026-05-08T10:30:00+08:00" "feat(feed): 首页信息流：召回 50 条，取前 20 条按时间倒序"

# ---------------------------------------------------------------- 5. dedup churn
sed -i.bak 's/duplicateThreshold = 0.85/duplicateThreshold = 0.88/' internal/dedup/dedup.go && rm internal/dedup/dedup.go.bak
commit "$ZHAO" "2026-05-19T09:15:00+08:00" "fix(dedup): 同一话题下观点不同的帖子被误合并，阈值调到 0.88"

# ---------------------------------------------------------------- 6. spam fix 2
cat > internal/moderation/banned.go <<'EOF'
package moderation

import (
	_ "embed"
	"regexp"
	"strings"
)

//go:embed regulated_words.txt
var regulatedWordsFile string

// regulatedWords 是监管违禁词，按 ADR 0002 必须精确匹配拦截。
var regulatedWords = loadWords(regulatedWordsFile)

// spamPatterns 识别引流、广告类评论。
var spamPatterns = []*regexp.Regexp{
	regexp.MustCompile(`加[微薇威]信[^支付]`), // “加微信支付优惠”是正常评论
	regexp.MustCompile(`(?i)v[x信]\s*[:：]?\s*[a-z0-9_-]{5,}`),
	regexp.MustCompile(`\+\s*[vV]\b`),
	regexp.MustCompile(`刷单`),
	regexp.MustCompile(`代开发票`),
}

func ContainsRegulatedWord(text string) bool {
	for _, w := range regulatedWords {
		if strings.Contains(text, w) {
			return true
		}
	}
	return false
}

func LooksLikeSpam(text string) bool {
	for _, p := range spamPatterns {
		if p.MatchString(text) {
			return true
		}
	}
	return false
}

func loadWords(file string) []string {
	var words []string
	for _, line := range strings.Split(file, "\n") {
		line = strings.TrimSpace(line)
		if line == "" || strings.HasPrefix(line, "#") {
			continue
		}
		words = append(words, line)
	}
	return words
}
EOF
commit "$ZHAO" "2026-06-03T14:00:00+08:00" "fix(moderation): 漏杀谐音“加薇信”“+V”引流评论"

# ---------------------------------------------------------------- 7. review sampling
cat > internal/review/sampler.go <<'EOF'
package review

import (
	"math/rand"

	"example.com/community/internal/model"
)

// sampleRate 是已通过机审的评论送人工抽检的比例。
const sampleRate = 0.02

type Queue interface {
	Enqueue(c model.Comment)
}

// MaybeSample 随机抽取一部分已通过机审的评论，交给审核员复查。
func MaybeSample(c model.Comment, q Queue) {
	if rand.Float64() < sampleRate {
		q.Enqueue(c)
	}
}
EOF
cat > internal/moderation/moderate.go <<'EOF'
package moderation

import (
	"example.com/community/internal/model"
	"example.com/community/internal/review"
)

type Verdict int

const (
	Publish Verdict = iota
	Block
)

// Moderate 对新评论做机审；通过的评论按比例抽检。
func Moderate(c model.Comment, q review.Queue) Verdict {
	if ContainsRegulatedWord(c.Text) {
		return Block
	}
	if LooksLikeSpam(c.Text) {
		return Block
	}
	review.MaybeSample(c, q)
	return Publish
}
EOF
cat > cmd/server/main.go <<'EOF'
package main

import (
	"encoding/json"
	"log"
	"net/http"

	"example.com/community/internal/model"
	"example.com/community/internal/moderation"
	"example.com/community/internal/review"
)

var reviewQueue review.Queue

func main() {
	http.HandleFunc("/api/comments", func(w http.ResponseWriter, r *http.Request) {
		var c model.Comment
		if err := json.NewDecoder(r.Body).Decode(&c); err != nil {
			http.Error(w, "bad request", http.StatusBadRequest)
			return
		}
		if moderation.Moderate(c, reviewQueue) == moderation.Block {
			http.Error(w, "评论包含违规内容", http.StatusForbidden)
			return
		}
		w.WriteHeader(http.StatusCreated)
	})
	log.Fatal(http.ListenAndServe(":8080", nil))
}
EOF
commit "$SUN" "2026-06-24T17:30:00+08:00" "feat(review): 已通过机审的评论按 2% 随机抽检"

# ---------------------------------------------------------------- 8. billing + auth
cat > internal/billing/price.go <<'EOF'
package billing

import "math"

// 推广帖按千次曝光计费，曝光越多折扣越大。
const cpmYuan = 12.0

var discountTiers = []struct {
	MinImpressions int
	Discount       float64
}{
	{1_000_000, 0.80},
	{100_000, 0.90},
	{0, 1.00},
}

// Price 返回推广费用（元），保留两位小数。
func Price(impressions int) float64 {
	discount := 1.0
	for _, tier := range discountTiers {
		if impressions >= tier.MinImpressions {
			discount = tier.Discount
			break
		}
	}
	raw := float64(impressions) / 1000 * cpmYuan * discount
	return math.Round(raw*100) / 100
}
EOF

cat > internal/auth/permission.go <<'EOF'
package auth

import "example.com/community/internal/model"

type Action string

const (
	DeleteComment Action = "delete_comment"
	BanUser       Action = "ban_user"
	EditPost      Action = "edit_post"
)

var rolePermissions = map[string][]Action{
	"admin":     {DeleteComment, BanUser, EditPost},
	"moderator": {DeleteComment, EditPost},
	"user":      {},
}

// Can 判断用户是否有权执行某个操作。
func Can(u model.User, a Action) bool {
	for _, allowed := range rolePermissions[u.Role] {
		if allowed == a {
			return true
		}
	}
	return false
}
EOF
commit "$WANG" "2026-07-10T11:00:00+08:00" "feat: 推广计费和角色权限"

# ---------------------------------------------------------------- 9. sampling rate change
sed -i.bak 's/sampleRate = 0.02/sampleRate = 0.05/' internal/review/sampler.go && rm internal/review/sampler.go.bak
commit "$SUN" "2026-07-29T10:20:00+08:00" "chore(review): 抽检比例从 2% 调到 5%" "上月用户举报的漏放评论里，大部分没有被抽到，审核员人手允许多看一些。"

# ---------------------------------------------------------------- 10. dedup churn back
sed -i.bak 's/duplicateThreshold = 0.88/duplicateThreshold = 0.85/' internal/dedup/dedup.go && rm internal/dedup/dedup.go.bak
commit "$ZHAO" "2026-08-14T16:40:00+08:00" "fix(dedup): 0.88 放过了太多搬运帖，阈值改回 0.85" "误合并的问题先由运营手工拆分。"

# ---------------------------------------------------------------- 11. spam fix 3 + tests
cat > internal/moderation/banned.go <<'EOF'
package moderation

import (
	_ "embed"
	"regexp"
	"strings"
)

//go:embed regulated_words.txt
var regulatedWordsFile string

// regulatedWords 是监管违禁词，按 ADR 0002 必须精确匹配拦截。
var regulatedWords = loadWords(regulatedWordsFile)

// spamPatterns 识别引流、广告类评论。
var spamPatterns = []*regexp.Regexp{
	regexp.MustCompile(`加[微薇威]信[^支付]`), // “加微信支付优惠”是正常评论
	regexp.MustCompile(`(?i)v[x信]\s*[:：]\s*[a-z0-9_-]{5,}`), // 必须带冒号，避免误杀显卡型号
	regexp.MustCompile(`\+\s*[vV]\b`),
	regexp.MustCompile(`刷单`),
	regexp.MustCompile(`代开发票`),
}

func ContainsRegulatedWord(text string) bool {
	for _, w := range regulatedWords {
		if strings.Contains(text, w) {
			return true
		}
	}
	return false
}

func LooksLikeSpam(text string) bool {
	for _, p := range spamPatterns {
		if p.MatchString(text) {
			return true
		}
	}
	return false
}

func loadWords(file string) []string {
	var words []string
	for _, line := range strings.Split(file, "\n") {
		line = strings.TrimSpace(line)
		if line == "" || strings.HasPrefix(line, "#") {
			continue
		}
		words = append(words, line)
	}
	return words
}
EOF

cat > internal/moderation/banned_test.go <<'EOF'
package moderation

import "testing"

func TestLooksLikeSpam(t *testing.T) {
	cases := []struct {
		text string
		want bool
	}{
		{"想要资料的加微信 abc12345", true},
		{"加薇信领红包", true},
		{"私聊+V", true},
		{"vx：hello_2026", true},
		{"用加微信支付有满减", false},
		{"这张显卡是 vx 5000 系列吗", false},
		{"兼职刷单日结，详情看主页", true},
		{"楼主说的刷单骗局我也遇到过，大家小心", false}, // 已知误杀，暂未修复
		{"主页有惊喜，懂的来", false},                // 已知漏杀，暂未修复
	}
	for _, c := range cases {
		if got := LooksLikeSpam(c.text); got != c.want {
			t.Errorf("LooksLikeSpam(%q) = %v, want %v", c.text, got, c.want)
		}
	}
}
EOF
commit "$ZHAO" "2026-09-02T10:05:00+08:00" "fix(moderation): 误杀讨论显卡型号“vx 5000”的评论，补充测试用例"
