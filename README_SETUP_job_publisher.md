# Job Page Auto Publisher — GitHub पर (PC/मोबाइल बंद रहने पर भी)

## यह क्या करता है
किसी job/result/admit-card notification का source लिंक (जैसे FreeJobAlert) दें → यह:
1. Title, vacancies, last date, category (Job/Result/Admit Card/Answer Key) खुद पहचानता है
2. AI (g4f, मुफ़्त) से पूरा SEO article लिखता है
3. `bgsarkariresult/bgsarkariresult` रिपो में सीधे नया HTML page push करता है (GitHub API से — local git clone की जरूरत नहीं)
4. `data/jobs.json` और `sitemap.xml` भी अपने आप update करता है
5. Telegram पर नई पोस्ट का लिंक भेज देता है

## जरूरी बात — GITHUB_TOKEN अलग से बनाना होगा
यह bot `bgsarkariresult/bgsarkariresult` रिपो में फाइलें बनाता/बदलता है। अगर यह workflow **किसी दूसरे** automation रिपो में चलेगी (उसी साइट रिपो में नहीं), तो GitHub का अपने-आप मिलने वाला टोकन काम नहीं करेगा — आपको एक **Personal Access Token (Classic)** बनाना होगा:

1. GitHub → Settings (अपने account की, repo की नहीं) → Developer settings → Personal access tokens → Tokens (classic) → Generate new token
2. Scope में सिर्फ **`repo`** टिक करें → Generate
3. जिस रिपो में यह workflow चलेगी, वहाँ Secret बनाएं: नाम **`GH_PAT`**, value वही टोकन

(अगर यह workflow खुद `bgsarkariresult/bgsarkariresult` रिपो के अंदर ही चलेगी, तो बताइए — तब PAT की जरूरत नहीं, built-in token से काम चल जाएगा, और मैं workflow थोड़ी आसान कर दूंगा।)

## रिपो में डालनी हैं ये फाइलें
```
full_automation_bot.py
requirements.txt
.github/workflows/job-page-publisher.yml
```

## Secrets (Settings → Secrets and variables → Actions)

| Secret Name          | Value                                      |
|------------------------|-----------------------------------------------|
| `GH_PAT`                | ऊपर बनाया गया Personal Access Token        |
| `TELEGRAM_BOT_TOKEN`    | Telegram बॉट token                            |
| `TELEGRAM_CHAT_ID`      | आपकी chat id (`@bglarenup` की जगह अपनी डालें, workflow में already हार्ड-कोडेड नहीं है) |

## इस्तेमाल कैसे करें
Actions → "Job Page Auto Publisher" → Run workflow → `job_url` में source notification का लिंक पेस्ट करें → Run।

कुछ ही सेकंड में page बनकर site पर live हो जाएगा और Telegram पर लिंक आ जाएगा।

## ध्यान रखने वाली बात
- यह bot सिर्फ **एक नया job page** बनाता है, वीडियो नहीं — वीडियो बनाने के लिए आपका मौजूदा `bg_auto_job_bot.py` (जो इसी बने हुए page का लिंक लेकर YouTube वीडियो बनाता है) अलग से चलाना होगा, या चाहें तो मैं दोनों को एक ही workflow में जोड़ दूं (page बने → उसका लिंक अपने आप `bg_auto_job_bot.py` को मिल जाए) — बताइए अगर यह चाहिए।
- AI (g4f) मुफ़्त/बिना key वाला है, इसलिए कभी-कभी धीमा या अस्थिर हो सकता है — fail होने पर कोड खुद एक अच्छा generic article बना देता है (blank नहीं छोड़ता)।
