# گزارش ریویو و اصلاح R1 — 0xBrain enrichment

تاریخ: 2026-10-10 · نقش: ریویوی مستقل نهایی و اصلاح R1 · دامنه: کد، آزمون، ابزار ارتقا؛ بدون دسترسی به VPS یا corpus واقعی.

این گزارش جای مجوز استقرار نیست. «کد آمادهٔ rehearsal روی کپی instance واقعی است» با «استقرار واقعی آمادهٔ ارتقاست» یکی نیست؛ دومی هنوز اثبات نشده است (بخش ۵).

## ۱. نسخه، شاخه و commitها

- مخزن: `doctordocto159753/0xbrain`.
- نسخهٔ مبنا (پیش از R1): `a245b3b2620800b1d2d6cc4be0dba6e44fb5b103` (= `main`).
- نسخهٔ ادغام‌شدهٔ بررسی‌شده (SHA شروع): `bf47a2b9547c5a5c8c85c2d85214bf7578b2c156`، سر `origin/claude/nice-fermat-lhs7lq` (PR #13، ۲۳ commit روی مبنا). تأیید شد که این، نسخهٔ ادغام‌شده است و نه `main` عمومی: `main` هنوز روی `a245b3b` است و هیچ فایل `scripts/brain_enrichment/` ندارد.
- شاخهٔ کار: `claude/affectionate-hypatia-17sjd2` (شاخهٔ تعیین‌شدهٔ این جلسه)، ساخته‌شده روی همان `bf47a2b`. شاخه‌ها و تغییرات دیگران دست‌کاری نشدند؛ برای شواهد baseline و integrated دو worktree جدا و detached در scratchpad ساخته شد.
- SHA پایان کد: `9477905d977a015c7a00f38396a1edd21014a716`. این گزارش در commit بعدی و فقط به‌صورت docs اضافه شده است. همه روی `origin/claude/affectionate-hypatia-17sjd2` push شده‌اند؛ هیچ merge به `main` انجام نشده است.
- commitهای اصلاحی (به ترتیب):
  - `c4af1e7` ترکیب annotationهای صفحه‌به‌صفحه (F1) و محدودکردن refresh ابتدای query (F2)
  - `22a555c` release از allowlist ابزار ارتقای خودش عبور می‌کند (F3)
  - `5b8b450` هشدار sidecar untracked پس از rollback سطح ۲ (F4) و salvage صفحه‌های ورودی در سطح ۳ (F5)
  - `4885bd8` منبع ویرایش‌شده دیگر «یافت نشد»ِ ساکت و «fresh» نیست (F6) + تست اثبات حفظ داده
  - `3bb325f` حفظ ترتیب صفحه‌ها پس از supplement؛ یک holding برای فایل ingest‌شده (F7)
  - `42e75bc` رسیدن `BRAIN_ENRICHMENT_MAX_INPUT_CHARS` به store (F8) + fuzz chunker + تست sidecar خراب
  - `bb8521a` benchmark: مقایسه با مسیر پیش‌فرض واقعی (QMD lexical) و حذف نشت نام فایل
  - `72da0a0` اسکریپت تکرارپذیر rehearsal واقعی Docker
  - `73ea562` اصلاحیه‌های CONTRACT-E1 و store-schema
  - `9477905` تست حفظ داده: ایندکس QMD جست‌وجوی پایه در `_search/qmd/` derived است، نه corpus
  - commit این گزارش (`docs/enrichment/REVIEW_FIX_REPORT.md`)

## ۲. یافته‌ها

هر یافته: علت → بازتولید → اصلاح → شاهد. regression testها در `tests/enrichment/test_r1_review_fixes.py` هستند. هر تستِ بازتولید پیش از اصلاح شکست و پس از اصلاح پاس شد؛ اینکه هر کدام چگونه پیش از اصلاح اجرا شد در بخش ۳ آمده است.

### P0

هیچ یافتهٔ P0 (ازدست‌رفتن داده یا نقض دسترسی) پیدا نشد. شاهدها در بخش ۴: hash کامل همهٔ فایل‌ها (شامل `.git`، untracked و ignored) پیرامون scan، annotation، index و retrieve؛ preflight فقط‌خواندنی روی Docker واقعی؛ rollback سطح ۲ که ورودی‌های پس از ارتقا و markerها را نگه داشت.

### P1

**F1 — metadata صفحه‌های قبلیِ یک منبع بلند ناپدید می‌شد.** Hermes یک منبع بلند را صفحه‌به‌صفحه annotate می‌کند و هر صفحه یک revision است (`harness/hermes/SKILL.md`). ایندکس، گراف و `brain_artifact_read` فقط جدیدترین revision را می‌خواندند. پس با submit صفحهٔ ۲، تگ‌ها، entityها و linkهای صفحهٔ ۱ از جست‌وجو حذف می‌شدند، در حالی که coverage می‌گفت «همهٔ چانک‌ها annotate شده‌اند». همچنین اگر revision آخر `rejected` بود، همهٔ revisionهای قبلی هم پنهان می‌شدند؛ این خلاف CONTRACT-E1 بخش ۱۱ است که می‌گوید «rejected فقط همان revision را پنهان می‌کند». بازتولید: `test_page_by_page_annotation_keeps_earlier_pages_metadata` و `test_rejecting_the_latest_page_hides_only_that_revision`. اصلاح: `store.compose_effective` که annotation مؤثرِ نسخهٔ جاری را می‌سازد. revisionهای rejected کنار گذاشته می‌شوند و پنجره از آخرین revision «کامل» شروع می‌شود. فیلدهای سطح artifact از جدیدترین revision می‌آیند، چانک‌ها به‌صورت per-chunk ترکیب می‌شوند و entity/linkها union هستند و revision مبدأ خود را نگه می‌دارند. کلیدهای قبلی item معنای خود را حفظ کرده‌اند (export و CLI بی‌تغییر) و کلیدهای جدید افزایشی‌اند. قرارداد در بخش ۵ اصلاح شد.

**F3 — ابزار ارتقا، release خودش را STOP می‌کرد.** روی rehearsal واقعی، `upgrade-enrichment.py preflight` با این خطا متوقف شد: `docs/UPGRADE.md` و `tests/conftest.py` خارج از allowlist هستند. این دو فایل در commitهای integration (`b152a2c`، `623d2bf`) تغییر کرده بودند، پس دستورهای runbook برای همین release هرگز اجراشدنی نبودند. گزارش integration این را ندیده بود، چون preflight و plan را روی diff واقعی release اجرا نکرده بود. اصلاح: هر دو فایل به `SHARED_FILES` (hunk مشترک، merge سه‌طرفه، محدود به تعداد خط) اضافه شدند. یک تست هم هر مسیر diff واقعی `a245b3b..HEAD` را کلاس‌بندی می‌کند تا این خطا دوباره رخ ندهد. پیامد برای گزارش نهایی: این گزارش در `docs/enrichment/` قرار گرفت، نه در ریشه، چون یک فایل در ریشه خودش release را STOP می‌کرد.

**F6 — منبعِ ویرایش‌شده «یافت نشد»ِ ساکت و fresh می‌داد.** پس از ویرایش رکورد canonical (کاری که promote انجام می‌دهد) یا افزوده‌شدن transcript به یک ویس، و پیش از discovery بعدی، `brain_retrieve` برای متن جدید این را برمی‌گرداند: صفر نتیجه، `index.fresh=true` و هیچ note. در همان حال `brain_search` قدیمی متن را پیدا می‌کرد. پرسش با متن قدیمی هم فقط `stale_dropped=1` بی‌توضیح می‌داد. این خلاف معیار «backfill ناقص متن و منابع را پنهان نکند» است. شمارش «تعداد فایل روی دیسک در برابر store» هم ویرایش را نمی‌بیند. بازتولید: `test_source_edited_after_indexing_is_found_before_the_next_discovery`. اصلاح: با index کامل و بدون cursor، صفحه‌ای که چانک stale را کنار گذاشته یا خالی مانده، از جست‌وجوی پایه کمک می‌گیرد. refهایی نگه داشته می‌شوند که ایندکس آن‌ها را ندارد یا نسخهٔ superseded دارد (با یک بار خواندن فایل)؛ این‌ها hit صریح سطح `source` و بدون `chunk_ref` ساختگی هستند. `fresh=false` و note علت را می‌گویند. cursor این وضعیت را حمل می‌کند تا صفحه‌های بعدی همان ترتیب را بسازند (`test_cursor_pages_keep_the_supplemented_order`؛ بدون این پرچم صفحهٔ ۲ hitها را گم می‌کرد). محدودیت باقی‌مانده: متنِ چانک‌سطح منبع ویرایش‌شده تا discovery بعدی (pending یا CLI scan) در دسترس نیست (بخش ۶).

### P2

**F2 — refresh ابتدای query محدود نبود.** هر `brain_retrieve` در حالت lag کل store را **دو بار** و با متن کامل می‌خواند (یک بار برای refresh و یک بار برای status)، بدون سقف زمانی و با انتظار برای قفل. اندازه‌گیری روی ۲۰۰۰ artifact کوچک: ۸۲ صفحهٔ `list_current` و حدود ۰٫۵۵ ثانیه برای هر query، که با حجم متن corpus خطی رشد می‌کند. writer دوم (مثلاً CLI در process دیگر) می‌توانست با cleanup «orphan rows» ردیف‌های generation در حال ساخت writer اول را حذف کند. این مورد استنباط از خواندن کد است و جداگانه بازتولید نشد؛ قفل آن را به‌هرحال منتفی می‌کند. اصلاح:
- اسکن بدون متن (`list_current_light`، با fingerprint یکسان) و خواندن متن فقط برای batch (`get_current_items`)؛
- استفادهٔ status از lag محاسبه‌شده؛
- سقف ۲ ثانیه برای نوشتن؛
- قفل غیرمسدودکنندهٔ درون‌پروسه‌ای به‌علاوهٔ `flock` بین‌پروسه‌ای روی `search-v1.sqlite3.lock` (CLI به‌صورت مسدودکننده آن را می‌گیرد).

پس از اصلاح: ۰ صفحهٔ متن کامل و حدود ۰٫۱۷ ثانیه برای هر query در همان آزمون. تست‌ها: `test_query_time_refresh_reads_no_text_beyond_its_batch`، `test_light_fingerprint_equals_full_fingerprint`، `test_query_time_refresh_never_waits_for_another_writer` و `test_query_time_refresh_time_cap_activates_what_it_wrote`. هزینهٔ باقی‌مانده: یک اسکن سبک O(N) ردیف پس از هر نوشتن در store.

**F4 — sidecar پس از rollback سطح ۲ untracked می‌شد.** `.gitignore` پیش از R1 برمی‌گردد، پس `_enrichment/` (کپی خصوصی متن corpus) untracked می‌شود. نویسنده‌های موجود (K9 با pathspec، و promote با prefixهای محتوایی) آن را برنمی‌دارند، ولی یک `git add -A` دستی آن را commit می‌کند. اصلاح: هشدار صریح با راه‌حل محلی (`.git/info/exclude`) و یک سطر در runbook؛ داده دست نمی‌خورد. این در rehearsal واقعی دیده و آزموده شد.

**F5 — salvage سطح ۳ صفحه‌های ورودی را جا می‌انداخت.** salvage فقط zoneهای corpus را با archive مقایسه می‌کرد. صفحه‌های ورودی (`HOME/README/SYSTEM_DESIGN/CLAUDE`)، که شمارنده‌هایشان با ingest پس از بکاپ تغییر می‌کند، salvage نمی‌شدند. اصلاح شد و تست دارد.

**F7 — یک holding دو بار فهرست می‌شد.** جست‌وجوی پایه برای فایل ingest‌شده ref مشتق (`doc:`) را برمی‌گرداند. وقتی index lag داشت، همان holding یک بار با `rec:` (چانک) و یک بار با `doc:` (سطح source) فهرست می‌شد؛ این خلاف CONTRACT-E1 بخش ۲ است که می‌گوید source record و derivative یک holding هستند. اصلاح: `IndexReader.artifact_by_text_ref`. تست: `test_a_held_file_is_one_holding_in_supplemented_results`.

**F8 — `BRAIN_ENRICHMENT_MAX_INPUT_CHARS` اعمال نمی‌شد.** factory واقعی store این مقدار را دریافت نمی‌کرد و سقف صفحهٔ متنِ عرضه‌شده به annotator همیشه ۲۴۰۰۰ بود. اصلاح شد و تست دارد.

**F9 — benchmark گمراه‌کننده بود.** «۰٫۳۲ → ۰٫۹۴» در گزارش integration با جست‌وجوی exact substring مقایسه شده بود، در حالی که پیش‌فرض واقعی `brain_search` جست‌وجوی QMD BM25 به‌همراه ladder است. افزودن baseline منصفانه یک نشت را هم آشکار کرد: نام فایل‌های fixture (`r_oral.md`، `c_fit.md`) واژه‌های پرسش را به QMD نشت می‌داد. نتیجهٔ منصفانه روی همان ۱۸ سند و ۲۶ پرسش ساختگی، با annotation دست‌نویس:
- `legacy-exact`: Recall@10 = 0.32، MRR = 0.36
- `legacy-lexical` (QMD، مسیر فعلی): 0.80 / 0.76، با p95 حدود ۲٫۷ ثانیه در این sandbox
- R1 بدون annotation: 0.80 / 0.84
- R1 با annotation: 0.94 / 0.96

برداشت درست: مسیر مستقیم R1 در recall برابر مسیر فعلی و در MRR بهتر است، و سودِ دیده‌شده عمدتاً از annotation دست‌نویس می‌آید. این عدد کیفیت تولیدی نیست.

### مواردی که بررسی شد و ایرادی نداشت

- **chunk و offset:** fuzz با ۳۰۰۰ متن تصادفی (فارسی، `\r\n`، `\r` تنها، پاراگراف بی‌مرز، جدول، front matter، page marker) هیچ نقضی نشان نداد: chunkها لایه را بی‌شکاف و بی‌همپوشانی partition می‌کنند، هر chunk substring دقیق است، سقف ۶۰۰۰ رعایت می‌شود، جفت `\r\n` شکسته نمی‌شود و خروجی قطعی است. نسخهٔ ۴۰۰تایی آن به‌عنوان تست ماندگار اضافه شد.
- **تفکیک ref و hash:** روی HTTP واقعی برای فایل ingest‌شده تأیید شد: `source_ref=rec:`، `text_ref=doc:`، tier `derivative` و level 5، بدون انتقال authority اصل فایل.
- **رد ورودی‌های جعلی:** quote جعلی و `actor` جعلی (`E_VALIDATION` و `E_UNKNOWN_ARGUMENT`، با `persisted:false`) رد می‌شوند؛ `chk:` در `brain_read` رد می‌شود؛ replay یکسان همان receipt را برمی‌گرداند.
- **سطح ابزار:** feature-off دقیقاً ۷ ابزار و feature-on دقیقاً ۷+۴ است (روی HTTP/OAuth واقعی). review هیچ مسیر MCP ندارد و آرگومان ناشناخته رد می‌شود.
- **خرابی sidecar:** فایل‌های store و index با بایت‌های خراب ابزارهای پایه را از کار نمی‌اندازند: capture، read، search و status کار می‌کنند، افزونه خطای تمیز بدون path یا traceback می‌دهد و retrieve به fallback صریح می‌رود.

## ۳. نتایج آزمون (PASS/FAIL/SKIP)

همه با Python 3.13.16 و SQLite 3.45.1 (FTS5 موجود)، در venv ایزوله (`requirements*.txt` و `deploy/requirements-image.txt`)، و همه در یک process: `python -m pytest -q -p no:cacheprovider -r fEs tests`.

- baseline `a245b3b` بدون QMD: ۳ FAIL، ۵۴۸ PASS، ۱۳ SKIP.
- integrated `bf47a2b` بدون QMD: ۳ FAIL، ۱۳۴۶ PASS، ۱۵ SKIP.
- baseline `a245b3b` با QMD 2.8.3 و `WIKI_RUN_SEARCH_EVAL=1`: ۵۶۴ PASS، ۰ FAIL، ۰ SKIP.
- integrated `bf47a2b` با QMD (worktree تمیز): ۱۳۶۳ PASS، ۰ FAIL، ۱ SKIP. تنها skip تست opt-in Docker بود.
- **HEAD نهایی** با QMD، eval جست‌وجو و `BRAIN_E2E_DOCKER=1`: **۱۳۸۱ PASS، ۰ FAIL، ۰ SKIP** در ۷۱۲ ثانیه، روی `9477905`. اجرا تنها و با `--basetemp` اختصاصی بود و `git status` پس از آن تمیز ماند.

**سه شکست baseline، با علت واحد و اثبات‌شده:**
- `tests/test_brain_fixtures.py::test_fixture[01-retrieve-prior-context]`
- `tests/test_brain_fixtures.py::test_fixture[05-provisional-relation]`
- `tests/test_remote_semantic.py::test_remote_tool_list_is_exact_and_flow_works_end_to_end`

در هر دو commit، دو مورد اول با این نتیجه شکست خوردند:
`{'ok': False, 'error': {'code': 'E_UNAVAILABLE', 'message': "lexical search index unavailable (qmd not installed); mode='exact' still works"}}`.
مورد سوم با `KeyError: 'groups'` در `test_remote_semantic.py:108` شکست خورد، چون همان `brain_search` lexical پاسخ `E_UNAVAILABLE` داد. وابستگی مفقود باینری `qmd` است. با نصب `@tobilu/qmd@2.8.3` (نسخهٔ pin‌شده در `Dockerfile` و CI) در prefix ایزولهٔ scratchpad و بدون نصب سراسری، هر سه تست روی **هر دو** commit پاس شدند. پس شکست‌ها محیطی‌اند و نه regression؛ و اکنون با وابستگی واقعی PASS هستند، نه با تغییر آزمون.

**SKIPها بدون QMD:** دوازده skip با «qmd not installed» و یکی با `WIKI_RUN_SEARCH_EVAL` (اجرای ۴ دقیقه‌ای). در integrated دو skip Docker هم بود: یکی opt-in و یکی «daemon not reachable». در اجرای نهایی daemon واقعی در دسترس بود و تست opt-in با image محلی اجرا شد. busybox به‌خاطر 429 از Docker Hub قابل pull نبود و از `BRAIN_E2E_IMAGE=python:3.13-slim-bookworm` استفاده شد.

**regression testهای review:** ۱۷ تست در `tests/enrichment/test_r1_review_fixes.py`.
- ده تست F1 تا F5 در یک اجرا روی `bf47a2b` شکست خوردند (`10 failed`).
- تست‌های F6، صفحه‌بندی پس از supplement، F7 و F8 هر کدام جداگانه، بدون اصلاح خودشان، شکست خوردند.
- سه تست دیگر نگهبان هستند (fuzz chunker، sidecar خراب، اثبات حفظ داده) و بازتولید باگ نیستند.
- روی HEAD هر ۱۷ تست، با و بدون QMD، پاس هستند.

**رخداد سمت ریویو (شفافیت):** دو اجرای suite در این جلسه نامعتبر شدند و دور ریخته شدند. علت هیچ‌کدام کد محصول نبود:
- چند جلسهٔ pytest را هم‌زمان اجرا کردم. pytest فقط سه پوشهٔ آخر `/tmp/pytest-of-root/pytest-N` را نگه می‌دارد، و جلسهٔ جدید پوشهٔ موقت جلسهٔ در حال اجرا را پاک کرد.
- اسکریپت probe خودم برای اثبات model-free مسیر جدید، در نخستین تلاش monkeypatch را روی attribute اشتباه زد و ۱۰ فایل fixture (capture) در `01-inbox/` در checkout توسعه نوشت. این فایل‌ها untracked بودند و fixture تست‌ها آن‌ها را کپی می‌کرد.

این مورد با `git status` کشف شد. فایل‌ها با تطبیق محتوا (متن‌های `seed_wiki`) و فهرست صریح حذف شدند. اجرای نهایی تنها، با `--basetemp` اختصاصی و روی working tree تمیز انجام شد. اجرای «integrated با QMD» در worktree جدا و پیش از این رخداد بود، پس از آن متأثر نیست.

**گیت‌ها:**
- `python scripts/validate_repo.py --full`: PASS (rc=0)
- `python scripts/validate_content_release.py`: PASS (rc=0)
- `python scripts/check_against_baseline.py`: `OK: validators clean.` (rc=0)

pre-commit gate (`core.hooksPath .githooks`) روی همهٔ commitها اجرا شد و «OK: validators clean» داد. هیچ baseline file یا validatorی برای عبور تضعیف نشد.

**تست‌های HTTP/OAuth:** `tests/enrichment/test_remote_extension_http.py` و `tests/test_remote_semantic.py` در suite پاس هستند؛ مورد دوم با QMD. فراتر از آن، کل جریان روی سرور واقعی در Docker و با OAuth واقعی اجرا شد (بخش ۴).

## ۴. حفاظت داده، HTTP/OAuth، QMD و Docker

**حفاظت داده.** در `test_scan_annotate_index_retrieve_change_no_corpus_byte`، sha256 و mode **هر فایل** (شامل `.git` و فایل‌های untracked و ignored) پیش و پس از scan، annotation، index و retrieve یکسان ماند. fixture شامل capture فارسی و CRLF، capture تکراری، ویس با و بی transcript، فایل ingest‌شده، source بدون derivative، استخراج `needs_ocr`، proposal ثبت‌نشده، یک فایل ignored و یک capture ثبت‌نشده است. تنها نوشتن‌ها در `_enrichment/` و `_search/enrichment/` هستند (به‌علاوهٔ ایندکس QMD جست‌وجوی پایه در `_search/qmd/`، وقتی `WIKI_QMD_HOME` تنظیم نشده باشد). سپس در همان تست:
- یک رکورد ویرایش و یک capture حذف شد، و یک capture تازه **وسط** یک pass backfill رسید؛
- چانک قدیمی `E_STALE_SOURCE` داد و چانک حذف‌شده `E_NOT_FOUND`؛
- capture تازه پیش از discovery به‌صورت hit سطح source و پس از آن به‌صورت چانک پیدا شد؛
- متن جدید رکورد پس از discovery برگشت؛
- و باز هیچ بایتی از corpus توسط لایهٔ enrichment تغییر نکرد.

**HTTP/OAuth واقعی** (سرور در کانتینر، با MCP SDK رسمی و consent واقعی owner):
- سطح ابزار و غنی‌سازی: ۷ ابزار در off و ۱۱ در on؛ submit، replay، quote جعلی و actor جعلی رد شدند؛ «فیت پلن» بدون ref با متن دقیق چانک پیدا شد، با `query_model_calls=0`؛ پرسش فقط‌تگ از مسیر metadata؛ متن مشتق با tier `derivative` و level 5.
- refresh توکن بدون consent جدید پس از apply، rollback ۱/۲ و restart موفق بود.

**QMD و model-free.** QMD 2.8.3 در prefix ایزوله نصب شد و `qmd 2.8.3 (facd35e)` گزارش داد.
- مسیر قدیمی: `python scripts/prove_search_model_free.py` نتیجهٔ **PASS** داد. لایه‌های ایزولاسیون فعال: HOME خالی، `unshare -rn` (network namespace بدون شبکه) و `strace -f`. نتیجه: ۰ باز شدن فایل مدل، ۰ connect غیرمحلی، بدون binding بومی llama، و cache فقط `index.sqlite` و `index.yml`.
- `python scripts/run_lexical_eval.py`: ladder+exact روی ۶۸ مورد hit@1 66 و hit@5 67، MRR 0.978 و ۰ false positive داد. دو شکست باقی‌مانده محدودیت‌های مستندشده‌اند (`exact-is-not-fuzzy` و `cross-language`).
- مسیر جدید: annotation، ساخت index و `brain_retrieve` زیر `unshare -rn` و `strace` اجرا شد. نتیجه: ۰ connect غیرمحلی، ۰ فایل مدل، `query_model_calls=0`، و `uses_candidate_annotations=true` که درست گزارش شد. تنها باینری‌های اجراشده git، sh و python بودند (از آماده‌سازی fixture)؛ هیچ `qmd` یا مدلی اجرا نشد.
- تفکیک حفظ شد: مسیر قدیمی model-free است؛ مسیر جدید annotation مدل را می‌خواند ولی در زمان query مدل صدا نمی‌زند.

**rehearsal واقعی Docker.**
- محیط: daemon ایزولهٔ Docker 29.8.2، compose v5.6.0 و overlayfs، داخل همین کانتینر توسعه.
- instance: یک project مستقل (`rh0xbrain`)، داده و auth آزمایشی، پورت فقط روی loopback (`127.0.0.1:18180`) و caddy غیرفعال با override (شبیه چیدمان production پشت Nginx). یک کانتینر شاهدِ بیرون از project روی پورت دیگر قرار گرفت.
- روند: instance پیش از R1 از `a245b3b` و `Dockerfile` واقعی ساخته شد، با `remote_smoke.py` و MCP دادهٔ واقعی گرفت، و سپس **ابزارهای واقعی** طبق runbook اجرا شدند.
- نتیجه: اسکریپت `docs/enrichment/rehearsal/docker_rehearsal.py` روی SHA نهایی `9477905` (و پیش از آن روی `72da0a0`، هر دو) **۴۷ چک و ۰ FAIL** داد. نکات کلیدی:
  - preflight فقط‌خواندنی بود (manifest کامل شامل `.git`، mode و owner بی‌تغییر؛ کانتینرها restart نشدند) و `tools/list` واقعی ۷ ابزار داد.
  - بکاپ `docker pause` کرد. restore روی scratch هر فایل wiki را بایت‌به‌بایت برگرداند (شامل capture و proposal ثبت‌نشده، `.env` و override) و auth یکسان بود. بکاپ دوم با store زنده در WAL از SQLite backup API استفاده کرد: `integrity ok`، تعداد ردیف‌ها برابر و هیچ فایل `-wal/-shm` خامی در archive نبود.
  - apply زنده فقط brain را recreate کرد، با همان image و همان port binding و شاهد دست‌نخورده؛ دقیقاً مسیرهای plan تغییر کردند. `verify` در off و on بدون FAIL بود.
  - rollback سطح ۱ ← ۷ ابزار. سطح ۲ ← همهٔ مسیرهای release به بایت‌های پیش از ارتقا برگشتند. ورودی‌های پس از ارتقا و markerهای `CORPUS_STATE` و صفحه‌های ورودی حفظ شدند، کد قدیمی آن‌ها را خواند و جست‌وجو کرد، و capture تازه با گیت pre-commit قدیمی commit شد. validatorهای قدیمی با وجود `_enrichment/` PASS بودند.
  - سطح ۳ ورودی‌های پس از بکاپ، صفحه‌های ورودی و snapshot store را salvage کرد، بدون تغییر در wiki زنده.
- انحراف‌های محیط که باید دانسته شوند:
  - proxy محیط فقط HTTPS CONNECT را می‌پذیرد، پس apt روی HTTP شکست خورد. image با `python:3.12-bookworm` (که git دارد) ساخته شد و git-lfs ندارد؛ production با slim و apt ساخته می‌شود.
  - `ss` در کانتینر نبود، پس baseline پورت‌های listening میزبان `UNVERIFIED` ماند.
  - این rehearsal یک instance تازه از kit است، **نه** کپی بازیابی‌شدهٔ instance نصب‌شدهٔ شما.

## ۵. وضعیت گیت‌ها

- **صحت R1 و قراردادهای قدیمی: PASS (روی fixture و rehearsal).** suite کامل بدون شکست است؛ ۸ یافتهٔ کد (F1 تا F8) و یک یافتهٔ benchmark (F9) اصلاح شد؛ ۷/۱۱ ابزار، refهای قدیمی، رد آرگومان ناشناخته و حفظ داده اثبات شدند. محدودیت: گزارش «ایرادی پیدا نشد» دربارهٔ همهٔ کد نیست، بلکه دربارهٔ مسیرهایی است که در بخش ۲ آمده.
- **QMD/model-free: PASS.** هر دو اسکریپت با QMD واقعی در محیط ایزولهٔ بدون شبکه اجرا شدند و مسیر جدید هم جداگانه اثبات شد.
- **rehearsal واقعی upgrade/rollback: PASS روی instance آزمایشی پیش از R1 در Docker واقعی.** روی **کپی بازیابی‌شدهٔ instance واقعی** هنوز NOT RUN است.
- **سازگاری instance نصب‌شده: BLOCKED.** دسترسی read-only به VPS یا snapshot خصوصی نبود. اختلاف کد و پیکربندی deployment (`93cbbdf` یا `d55a085`) با `a245b3b` شناخته نشده است. preflight آمادهٔ اجراست.
- **کیفیت retrieval روی دادهٔ واقعی: BLOCKED.** فقط benchmark ساختگیِ هم‌مؤلف با annotation دست‌نویس داریم. هدف +۱۰ واحد اثبات‌شده نیست و خروجی واقعی Hermes هم وجود ندارد.

جمع‌بندی: **کد آمادهٔ rehearsal روی کپی instance واقعی است؛ استقرار واقعی هنوز آمادهٔ ارتقا اعلام نمی‌شود.** R1.1 (plugin فایل و ویس تلگرام، UI review، author confirmation) و R2 (embedding و LightRAG) خارج از این مأموریت ماندند و موفق یا کامل گزارش نمی‌شوند.

## ۶. blockerها، ریسک‌های باقی‌مانده و عملیات بعدی

**blockerها:**
1. سازگاری instance نصب‌شده؛ نیازمند اپراتور.
2. rehearsal روی کپی بازیابی‌شدهٔ واقعی.
3. benchmark روی دادهٔ واقعی.
4. قالب واقعی job در Hermes، loader YAML و OAuth helper آن هرگز روی Hermes واقعی آزموده نشده‌اند.

**ریسک‌های باقی‌مانده (اصلاح‌نشده، آگاهانه):**
- تازگی چانک‌ها به discovery وابسته است (pending یا CLI scan). F6 «یافت نشد»ِ ساکت را بسته است، ولی متن چانک‌سطح منبع ویرایش‌شده تا pass بعدی نمی‌آید. پیشنهاد: یک `cli scan` دوره‌ای مستقل از Hermes (cron روی همان کاربر)، تا توقف اعتبار مدل discovery را متوقف نکند.
- اسکن سبک index پس از هر نوشتن در store O(N) ردیف است (حدود ۰٫۱ ثانیه برای ۲۰۰۰ artifact)، و `_disk_total` در هر query دایرکتوری‌ها را stat می‌کند.
- توکن OAuth، Hermes را به هر ۱۱ ابزار (از جمله capture و propose) مجاز می‌کند و محدودسازی ابزار فقط در harness است، نه در سرور.
- پس از rollback سطح ۲، `_enrichment/` باید از `git add -A` دور بماند (F4).

**عملیات بعدی دقیق (به ترتیب):**
1. روی VPS، فقط‌خواندنی، از یک clone جدا از release روی `9477905d977a015c7a00f38396a1edd21014a716`:
   `python scripts/upgrade-enrichment.py preflight --wiki /opt/0xbrain/wiki --release-repo <clone> --target-sha 9477905d977a015c7a00f38396a1edd21014a716 --base-sha a245b3b2620800b1d2d6cc4be0dba6e44fb5b103 --out <private>/preflight.json --token-file <private>/token`
   سپس `docs/enrichment/compat-report.template.md` را پر کنید و خلاصهٔ redactشده را برگردانید. اگر `head` instance از `a245b3b` نیست، diff کد و پیکربندی (`compose*.yaml`، `Dockerfile`، `deploy/`، `scripts/`، پنل احتمالی) پیش از هر plan بررسی شود.
2. بکاپ (`backup`، سپس `enrichment-backup.py verify`) و restore آن روی یک میزبان staging. در آنجا `plan`، `apply --copy-dir` و `rollback --level 2 --copy-dir`، و سپس rehearsal زنده با یک project جدا و بدون پورت عمومی اجرا شود؛ اسکریپت `docs/enrichment/rehearsal/` الگوی چک‌هاست.
3. نصب با `BRAIN_ENRICHMENT=off`، سپس `verify --expect-features off`، سپس shadow و pilot ده‌تایی، طبق `runbook.md`.
4. benchmark واقعی: ۳۰ تا ۵۰ پرسش واقعی شما، همراه منابع مرتبطی که خودتان پیش از دیدن خروجی سیستم‌ها مشخص می‌کنید. شامل زیرمجموعهٔ وابسته به metadata، پرسش‌های منفی و ترکیب فارسی/انگلیسی. سه سیستم روی همان snapshot مقایسه شوند: `legacy-lexical` (پیش‌فرض فعلی)، `r1-direct` و `r1` با annotation **واقعی** Hermes. معیارها: Recall@10، MRR، نرخ «نیست»ِ اشتباه و faithfulness ارجاع، به‌علاوهٔ latency و هزینه. پرسش‌ها پیش از اجرا freeze شوند و annotation دست‌نویس در این مقایسه استفاده نشود.
