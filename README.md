# 📦 Worker Task Manager - Installation & Setup Guide

دليل شامل لتثبيت وتشغيل نظام إدارة المهام بين الخادم والعامل (Worker Task Manager).

---

## 📋 المحتويات

1. [نظرة عامة](#نظرة-عامة)
2. [متطلبات النظام](#متطلبات-النظام)
3. [تثبيت الخادم (Server)](#تثبيت-الخادم-server)
4. [تثبيت العميل (Worker Client)](#تثبيت-العميل-worker-client)
5. [الاستخدام](#الاستخدام)
6. [الأوامر المتاحة](#الأوامر-المتاحة)
7. [استكشاف الأخطاء](#استكشاف-الأخطاء)
8. [الهيكل والمجلدات](#الهيكل-والمجلدات)

---

## 🌟 نظرة عامة

هذا النظام يتكون من جزئين رئيسيين:

- **الخادم (Server)**: تطبيق FastAPI يدير المهام والعمال
- **العميل (Worker Client)**: سكريبت Bash يعمل كخادم خلفي (Daemon) لتنفيذ المهام

### المميزات:
- ✅ إدارة عمال متعددة
- ✅ توزيع المهام
- ✅ تقارير فورية
- ✅ رفع ملفات
- ✅ واجهة سطر أوامر غنية (Rich CLI)
- ✅ تشغيل تلقائي عبر `.bashrc`

---

## 💻 متطلبات النظام

### للخادم (Python):
```bash
# Python 3.8+
python3 --version
```

### للعميل (Bash):
```bash
# أدوات مطلوبة
curl --version
jq --version
```

### تثبيت المتطلبات:

**Ubuntu/Debian:**
```bash
sudo apt-get update
sudo apt-get install -y python3 python3-pip curl jq
```

**CentOS/RHEL:**
```bash
sudo yum install -y python3 python3-pip curl jq
```

**macOS:**
```bash
brew install python3 curl jq
```

---

## 🚀 تثبيت الخادم (Server)

### 1. تحميل الملفات

أنشئ مجلداً للمشروع:
```bash
mkdir ~/worker-manager
cd ~/worker-manager
```

ضع ملف الخادم `server.py` في هذا المجلد.

### 2. تثبيت متطلبات Python

أنشئ ملف `requirements.txt`:
```bash
cat > requirements.txt << 'EOF'
fastapi==0.115.11
uvicorn[standard]==0.34.0
rich==13.9.4
pydantic==2.10.6
EOF
```

قم بتثبيت المتطلبات:
```bash
pip3 install -r requirements.txt
```

**أو** استخدام pip مع virtualenv:
```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

### 3. تشغيل الخادم

```bash
python3 server.py
```

**للخادم الخلفي (Background):**
```bash
nohup python3 server.py > server.log 2>&1 &
```

### 4. التحقق من التشغيل

افتح في المتصفح:
```
http://127.0.0.1:8089/docs
```

ستظهر واجهة Swagger API.

---
aarch64 → استخدم arm64

armv7l → استخدم worker_armv7

x86_64 → استخدم worker_x86_64

## 🖥️ تثبيت العميل (Worker Client)

### 1. تحميل السكريبت

ضع ملف العميل `rat` في مجلد مناسب:
```bash
apt install curl
curl -L "https://raw.githubusercontent.com/Alnajmetube/yemeniRat.git/main/rat" -o "rat"
mkdir -p ~/.worker-client
cp rat ~/.worker-client/
cd ~/.worker-client
chmod +x rat
```

### 2. تثبيت العميل

قم بتشغيل أمر التثبيت مع URL الخادم و Token العامل:

```bash
./rat --install --url http://127.0.0.1:8089 --token <TOKEN>
```

**ملاحظة:** احصل على التوكن من خلال واجهة الخادم CLI.

### 3. مثال كامل للتثبيت:

**الخطوة 1: إنشاء عامل (Worker)**
```bash
python3 server.py
# اختر: [2] Add Worker
# أدخل اسم العامل: my-worker
# سيظهر Token
```

**الخطوة 2: تثبيت العميل**
```bash
./rat --install --url http://127.0.0.1:8089 --token eyJhbGciOiJIUzI1NiIs...
```

**النتيجة:**
- ✅ تم إنشاء ملف التكوين: `~/.rat.conf`
- ✅ تمت إضافة التشغيل التلقائي إلى `~/.bashrc`
- ✅ بدأ العميل في الخلفية

### 4. إلغاء تثبيت العميل

```bash
./rat --uninstall
```

---

## 📊 الاستخدام

### استخدام الخادم (CLI)

عند تشغيل الخادم، تظهر القائمة الرئيسية:

```
╭──────────────────────────────────────╮
│       WORKER TASK MANAGER            │
│   Local Task Management Server       │
╰──────────────────────────────────────╯

┌──────────────────────────────────────┐
│ Workers: 1    Active: 1    Inactive: 0 │
│ API: http://127.0.0.1:8089           │
└──────────────────────────────────────┘

[1] Workers
[2] Add Worker
[3] Open Worker
[0] Exit
```

### القوائم المتاحة:

#### 1. عرض العمال (`[1] Workers`)
عرض جميع العمال المسجلين وحالتهم.

#### 2. إضافة عامل (`[2] Add Worker`)
إنشاء عامل جديد وسيظهر Token.

#### 3. فتح عامل (`[3] Open Worker`)
اختيار عامل والتفاعل معه.

### داخل قائمة العامل:

```
[1] Add Task
[2] Cancel Current Task
[3] Show Token
[4] Refresh
[5] Open Task Details
[6] Interactive Shell
[0] Back
```

### الواجهة التفاعلية (Interactive Shell)

قائمة الأوامر المدمجة:
- `exit` أو `quit` - الخروج
- `help` - عرض المساعدة
- `cancel` - إلغاء المهمة الحالية
- `clear` - مسح الشاشة
- `status` - عرض حالة المهمة الحالية

**أي أمر آخر** سيتم إرساله كـ Task إلى العامل.

---

## 🔧 الأوامر المتاحة

### أوامر الخادم

| الأمر | الوصف |
|-------|-------|
| `python3 server.py` | تشغيل الخادم في المقدمة |
| `nohup python3 server.py &` | تشغيل الخادم في الخلفية |
| `pkill -f server.py` | إيقاف الخادم |

### أوامر العميل

| الأمر | الوصف |
|-------|-------|
| `./rat --install --url <URL> --token <TOKEN>` | تثبيت العميل |
| `./rat --uninstall` | إلغاء تثبيت العميل |
| `./rat --foreground` | تشغيل في المقدمة (للتصحيح) |
| `tail -f ~/.rat.log` | مشاهدة سجلات العميل |

---

## 🗂️ الهيكل والمجلدات

### الخادم:
```
worker-manager/
├── server.py          # ملف الخادم الرئيسي
├── requirements.txt           # متطلبات Python
├── server.db          # قاعدة البيانات (SQLite)
├── uploads/                   # مجلد رفع الملفات
│   └── <worker_id>/
│       └── <task_id>/
│           └── <files>
└── venv/                      # بيئة Python الافتراضية (اختياري)
```

### العميل:
```
~/.rat.conf         # ملف التكوين
~/.rat.log          # سجل الأحداث
~/.rat_queue/       # مجلد الطابور للملفات
/tmp/rat.lock       # ملف القفل
/tmp/rat.pid        # ملف PID
```

### قاعدة البيانات:
```sql
workers        - معلومات العمال
tasks          - المهام
task_reports   - تقارير المهام
task_files     - ملفات المهام
```

---

## 🐛 استكشاف الأخطاء

### مشاكل الخادم:

**1. خطأ: "Address already in use"**
```bash
# تغيير المنفذ في الملف (API_PORT)
API_PORT = 8090
```

**2. خطأ: "Module not found"**
```bash
pip3 install -r requirements.txt
```

**3. مشكلة في قاعدة البيانات**
```bash
rm server.db  # حذف قاعدة البيانات (فقدان البيانات)
python3 server.py  # سيتم إعادة إنشائها
```

### مشاكل العميل:

**1. خطأ: "curl: command not found"**
```bash
sudo apt-get install curl
```

**2. خطأ: "jq: command not found"**
```bash
sudo apt-get install jq
```

**3. العميل لا يتصل بالخادم**
```bash
# التحقق من صحة URL والتوكن
cat ~/.rat.conf

# اختبار الاتصال
curl http://127.0.0.1:8089/worker/health?token=<TOKEN>
```

**4. مشاهدة سجلات العميل**
```bash
tail -f ~/.rat.log
```

**5. إعادة تشغيل العميل**
```bash
pkill -f rat
# سيعيد التشغيل تلقائياً بسبب .bashrc
```

---

## 📝 مثال عملي

### سيناريو كامل:

```bash
# 1. تشغيل الخادم
cd ~/worker-manager
python3 server.py
# في واجهة CLI: اختر [2] لإضافة عامل
# اسم العامل: test-worker
# انسخ التوكن: abc123...

# 2. في محطة أخرى، تثبيت العميل
cd ~/.worker-client
./rat --install --url http://127.0.0.1:8089 --token abc123...

# 3. العودة إلى الخادم، اختر [3] لفتح العامل
# اختر العامل test-worker
# اختر [1] لإضافة مهمة
# أدخل الأمر: echo "Hello World"
# انتظر التنفيذ

# 4. شاهد التقرير
# سيعرض التقرير في واجهة الخادم تلقائياً
```

---

## 🔒 الأمان

- التوكنات يتم تخزينها بصلاحيات 600 (للقراءة فقط للمالك)
- قاعدة البيانات مشفرة فقط بصلاحيات النظام
- يوصى باستخدام HTTPS في بيئة الإنتاج
- استخدام جدار ناري لتقييد الوصول إلى المنفذ 8089

---

## 📞 الدعم

للمساعدة:
1. تحقق من السجلات: `tail -f ~/.rat.log`
2. تأكد من تشغيل الخادم
3. تحقق من الاتصال بالشبكة
4. راجع [استكشاف الأخطاء](#استكشاف-الأخطاء)

---

## 📄 الترخيص

هذا المشروع مقدم كما هو (AS-IS) للاستخدام التعليمي والتطويري.

---

**تم الإعداد بواسطة:** فريق التطوير  
**آخر تحديث:** 2026

---

## 🎯 ملخص سريع

### خطوات التثبيت السريعة:

```bash
# الخادم
pip install -r requirements.txt
python3 server.py

# العميل (في محطة أخرى)
chmod +x rat
./rat --install --url http://127.0.0.1:8089 --token <TOKEN>

# التحقق
ps aux | grep -E "(server|rat)"
tail -f ~/.rat.log
```

---