import os
from datetime import date
from typing import List, Optional

from fastapi import FastAPI, UploadFile, File, HTTPException, Depends, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials # ←追加
from pydantic import BaseModel, EmailStr
from supabase import create_client, Client
from dotenv import load_dotenv

load_dotenv()

app = FastAPI(
    title="CAMPUS MATCH API",
    description="大阪大学限定マッチングアプリのバックエンドAPIです。認証、プロフィール管理、マッチング、ブロック、チャット、通報、退会機能などを提供します。",
    version="1.2.1"
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"], 
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

# --- セキュリティ設定 ---
security = HTTPBearer() # ←追加

# --- データモデル ---

class UserRegistration(BaseModel):
    email: EmailStr
    password: str
    birthday: date
    gender: str
    is_graduate: bool
    department: str
    major: str
    student_id: str
    phone: str
    agreed_to_terms: bool

class UserLogin(BaseModel):
    email: EmailStr
    password: str

class ProfileSetup(BaseModel):
    nickname: str
    bio: Optional[str] = None
    habit: List[str]
    circles: Optional[str] = None
    free_slots: List[str]

class ProfileUpdate(BaseModel):
    nickname: Optional[str] = None
    bio: Optional[str] = None
    habit: Optional[List[str]] = None
    circles: Optional[str] = None
    free_slots: Optional[List[str]] = None

class LikeRequest(BaseModel):
    to_user_id: str

class BlockRequest(BaseModel):
    target_user_id: str

class ReportRequest(BaseModel):
    target_user_id: str
    reason: str

class MessageCreate(BaseModel):
    match_id: int
    content: str

class DeviceTokenRequest(BaseModel):
    token: str

# --- 共通処理 ---

async def get_current_user(credentials: HTTPAuthorizationCredentials = Depends(security)):
    """
    ヘッダーのBearerトークンを検証してユーザー情報を取得します。
    Swagger UIでは右上の「Authorize」ボタンからトークンを設定してください。
    """
    try:
        # credentials.credentials には "Bearer " が除かれたトークン本体が入っています
        token = credentials.credentials
        user_response = supabase.auth.get_user(token)
        if not user_response.user:
            raise HTTPException(status_code=401, detail="認証に失敗しました")
        return user_response.user
    except Exception:
        raise HTTPException(status_code=401, detail="有効なトークンが必要です")

def calculate_age(birthday: date) -> int:
    today = date.today()
    return today.year - birthday.year - ((today.month, today.day) < (birthday.month, birthday.day))

async def get_blocked_user_ids(current_user_id: str) -> List[str]:
    res = supabase.table("blocks").select("blocker_id, blocked_id").or_(
        f"blocker_id.eq.{current_user_id},blocked_id.eq.{current_user_id}"
    ).execute()
    
    blocked_ids = set()
    for row in res.data:
        blocked_ids.add(row['blocker_id'])
        blocked_ids.add(row['blocked_id'])
    
    if current_user_id in blocked_ids:
        blocked_ids.remove(current_user_id)
        
    return list(blocked_ids)

# --- 認証・アカウント関連 ---

@app.post("/auth/signup", summary="新規アカウント登録", tags=["認証"])
async def signup(user_info: UserRegistration):
    if not user_info.agreed_to_terms:
        raise HTTPException(status_code=400, detail="利用規約およびプライバシーポリシーへの同意が必要です")

    try:
        res = supabase.auth.sign_up({"email": user_info.email, "password": user_info.password})
        if not res.user:
            raise HTTPException(status_code=400, detail="ユーザー作成に失敗しました")

        profile_data = {
            "id": res.user.id,
            "email": user_info.email,
            "birthday": str(user_info.birthday),
            "gender": user_info.gender,
            "is_graduate": user_info.is_graduate,
            "department": user_info.department,
            "major": user_info.major,
            "student_id": user_info.student_id,
            "phone": user_info.phone,
            "agreed_to_terms": user_info.agreed_to_terms
        }
        supabase.table("profiles").insert(profile_data).execute()
        return {"message": "基本情報登録完了。メールを確認してください。", "user_id": res.user.id}
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))

@app.post("/auth/login", summary="ログイン", tags=["認証"])
async def login(credentials: UserLogin):
    try:
        res = supabase.auth.sign_in_with_password({"email": credentials.email, "password": credentials.password})
        return {"access_token": res.session.access_token, "user_id": res.user.id}
    except Exception:
        raise HTTPException(status_code=401, detail="メールアドレスまたはパスワードが正しくありません")

@app.delete("/auth/withdraw", summary="退会処理", tags=["認証"])
async def withdraw(user=Depends(get_current_user)):
    supabase.table("profiles").delete().eq("id", user.id).execute()
    return {"message": "退会処理が完了しました。ご利用ありがとうございました。"}

@app.post("/auth/upload-student-id", summary="学生証のアップロード", tags=["認証・本人確認"])
async def upload_student_id(file: UploadFile = File(...), user=Depends(get_current_user)):
    file_content = await file.read()
    file_ext = file.filename.split(".")[-1]
    file_path = f"verifications/{user.id}_student_id.{file_ext}"
    supabase.storage.from_("verification").upload(path=file_path, file=file_content, file_options={"content-type": file.content_type, "x-upsert": "true"})
    return {"message": "学生証をアップロードしました。審査をお待ちください。"}

# --- ユーザー検索・詳細 ---

@app.get("/users/search", summary="ユーザーの絞り込み検索", tags=["ユーザー"])
async def search_users(
    gender: Optional[str] = Query(None, description="性別でフィルタリング"),
    department: Optional[str] = Query(None, description="学部でフィルタリング"),
    is_graduate: Optional[bool] = Query(None, description="学部生か院生かでフィルタリング"),
    habit: Optional[str] = Query(None, description="趣味（カンマ区切り）で部分一致検索"),
    user=Depends(get_current_user)
):
    blocked_ids = await get_blocked_user_ids(user.id)
    query = supabase.table("profiles").select("*").neq("id", user.id)
    
    if blocked_ids:
        query = query.not_.in_("id", blocked_ids)
    
    if gender: query = query.eq("gender", gender)
    if department: query = query.eq("department", department)
    if is_graduate is not None: query = query.eq("is_graduate", is_graduate)
    
    res = query.execute()
    results = res.data

    if habit:
        search_habits = [h.strip() for h in habit.split(",")]
        results = [u for u in results if u.get('habit') and any(sh in u['habit'] for sh in search_habits)]

    for u in results:
        if u['birthday']: u['age'] = calculate_age(date.fromisoformat(u['birthday']))
            
    return results

@app.get("/users/{user_id}", summary="ユーザー詳細情報の取得", tags=["ユーザー"])
async def get_user_detail(user_id: str, user=Depends(get_current_user)):
    blocked_ids = await get_blocked_user_ids(user.id)
    if user_id in blocked_ids:
        raise HTTPException(status_code=404, detail="ユーザーが見つかりません")

    res = supabase.table("profiles").select("*").eq("id", user_id).single().execute()
    if not res.data:
        raise HTTPException(status_code=404, detail="ユーザーが見つかりません")
    
    data = res.data
    if data['birthday']: data['age'] = calculate_age(date.fromisoformat(data['birthday']))
    return data

# --- プロフィール管理 ---

@app.post("/profile/setup", summary="詳細プロフィールの初回設定", tags=["プロフィール"])
async def setup_detailed_profile(data: ProfileSetup, user=Depends(get_current_user)):
    update_data = {"nickname": data.nickname, "bio": data.bio, "habit": data.habit, "circles": data.circles, "free_slots": data.free_slots}
    res = supabase.table("profiles").update(update_data).eq("id", user.id).execute()
    return {"message": "プロフィールを保存しました", "data": res.data}

@app.put("/profile/me", summary="プロフィールの設定変更", tags=["プロフィール"])
async def update_my_profile(data: ProfileUpdate, user=Depends(get_current_user)):
    update_data = {k: v for k, v in data.dict().items() if v is not None}
    if not update_data: raise HTTPException(status_code=400, detail="更新するデータがありません")
    res = supabase.table("profiles").update(update_data).eq("id", user.id).execute()
    return {"message": "プロフィールを更新しました", "data": res.data}

@app.post("/profile/upload-avatar", summary="プロフィール写真のアップロード", tags=["プロフィール"])
async def upload_avatar(file: UploadFile = File(...), user=Depends(get_current_user)):
    file_content = await file.read()
    file_ext = file.filename.split(".")[-1]
    file_path = f"{user.id}/avatar.{file_ext}"
    
    supabase.storage.from_("avatars").upload(path=file_path, file=file_content, file_options={"content-type": file.content_type, "x-upsert": "true"})
    image_url = supabase.storage.from_("avatars").get_public_url(file_path)
    
    supabase.table("profiles").update({"photo_url": image_url}).eq("id", user.id).execute()
    return {"message": "プロフィール写真を更新しました", "url": image_url}

@app.post("/profile/device-token", summary="プッシュ通知用トークンの登録", tags=["プロフィール"])
async def register_device_token(req: DeviceTokenRequest, user=Depends(get_current_user)):
    supabase.table("profiles").update({"device_token": req.token}).eq("id", user.id).execute()
    return {"message": "プッシュ通知の設定を保存しました"}

# --- アクション ---

@app.get("/interactions/matches", summary="マッチング一覧の取得", tags=["アクション"])
async def get_my_matches(user=Depends(get_current_user)):
    res = supabase.table("matches").select("*, profiles!user_a(*), profiles!user_b(*)").or_(
        f"user_a.eq.{user.id},user_b.eq.{user.id}"
    ).execute()
    
    formatted_matches = []
    for m in res.data:
        other_profile = m['profiles!user_b'] if m['user_a'] == user.id else m['profiles!user_a']
        if other_profile['birthday']:
            other_profile['age'] = calculate_age(date.fromisoformat(other_profile['birthday']))
        formatted_matches.append({"match_id": m['id'], "partner": other_profile, "matched_at": m['created_at']})
    return formatted_matches

@app.post("/interactions/like", summary="いいね送信・マッチング判定", tags=["アクション"])
async def like_user(req: LikeRequest, user=Depends(get_current_user)):
    blocked_ids = await get_blocked_user_ids(user.id)
    if req.to_user_id in blocked_ids:
        raise HTTPException(status_code=400, detail="この操作は行えません")

    try:
        supabase.table("likes").insert({"from_id": user.id, "to_id": req.to_user_id}).execute()
    except Exception:
        return {"message": "既にいいね済みです"}

    reverse = supabase.table("likes").select("*").eq("from_id", req.to_user_id).eq("to_id", user.id).execute()
    is_match = len(reverse.data) > 0
    
    if is_match:
        supabase.table("matches").insert({"user_a": user.id, "user_b": req.to_user_id}).execute()

    return {"is_match": is_match, "message": "マッチングしました！" if is_match else "いいねを送信しました"}

@app.delete("/interactions/unmatch/{match_id}", summary="マッチングの解除", tags=["アクション"])
async def unmatch(match_id: int, user=Depends(get_current_user)):
    match_res = supabase.table("matches").select("*").eq("id", match_id).execute()
    if not match_res.data:
        raise HTTPException(status_code=404, detail="マッチングが見つかりません")
    m = match_res.data[0]
    if m['user_a'] != user.id and m['user_b'] != user.id:
        raise HTTPException(status_code=403, detail="権限がありません")

    supabase.table("matches").delete().eq("id", match_id).execute()
    supabase.table("likes").delete().or_(
        f"and(from_id.eq.{m['user_a']},to_id.eq.{m['user_b']}),and(from_id.eq.{m['user_b']},to_id.eq.{m['user_a']})"
    ).execute()
    return {"message": "マッチングを解除しました"}

@app.post("/interactions/block", summary="ユーザーのブロック", tags=["アクション"])
async def block_user(req: BlockRequest, user=Depends(get_current_user)):
    if req.target_user_id == user.id:
        raise HTTPException(status_code=400, detail="自分自身をブロックすることはできません")
    
    try:
        supabase.table("blocks").insert({"blocker_id": user.id, "blocked_id": req.target_user_id}).execute()
        supabase.table("matches").delete().or_(
            f"and(user_a.eq.{user.id},user_b.eq.{req.target_user_id}),and(user_a.eq.{req.target_user_id},user_b.eq.{user.id})"
        ).execute()
        return {"message": "ユーザーをブロックしました"}
    except Exception:
        return {"message": "既にブロック済みです"}

@app.delete("/interactions/block/{target_user_id}", summary="ブロックの解除", tags=["アクション"])
async def unblock_user(target_user_id: str, user=Depends(get_current_user)):
    supabase.table("blocks").delete().eq("blocker_id", user.id).eq("blocked_id", target_user_id).execute()
    return {"message": "ブロックを解除しました"}

@app.post("/interactions/report", summary="悪質なユーザーの通報", tags=["アクション"])
async def report_user(req: ReportRequest, user=Depends(get_current_user)):
    supabase.table("reports").insert({
        "reporter_id": user.id,
        "reported_id": req.target_user_id,
        "reason": req.reason
    }).execute()
    return {"message": "運営に通報を送信しました。調査を行います。"}

# --- チャット関連 ---

@app.get("/chat/{match_id}/messages", summary="メッセージ履歴の取得", tags=["チャット"])
async def get_messages(match_id: int, user=Depends(get_current_user)):
    match_res = supabase.table("matches").select("*").eq("id", match_id).execute()
    if not match_res.data or (match_res.data[0]['user_a'] != user.id and match_res.data[0]['user_b'] != user.id):
        raise HTTPException(status_code=403, detail="チャットの閲覧権限がありません")

    res = supabase.table("messages").select("*").eq("match_id", match_id).order("created_at").execute()
    return res.data

@app.post("/chat/send", summary="メッセージの送信", tags=["チャット"])
async def send_message(msg: MessageCreate, user=Depends(get_current_user)):
    match_res = supabase.table("matches").select("*").eq("id", match_id := msg.match_id).execute()
    if not match_res.data or (match_res.data[0]['user_a'] != user.id and match_res.data[0]['user_b'] != user.id):
        raise HTTPException(status_code=403, detail="メッセージを送信する権限がありません")

    res = supabase.table("messages").insert({
        "match_id": msg.match_id,
        "sender_id": user.id,
        "content": msg.content
    }).execute()
    return res.data[0]

@app.put("/chat/{match_id}/read", summary="メッセージの既読処理", tags=["チャット"])
async def mark_messages_as_read(match_id: int, user=Depends(get_current_user)):
    match_res = supabase.table("matches").select("*").eq("id", match_id).execute()
    if not match_res.data or (match_res.data[0]['user_a'] != user.id and match_res.data[0]['user_b'] != user.id):
        raise HTTPException(status_code=403, detail="権限がありません")

    supabase.table("messages").update({"is_read": True}).eq("match_id", match_id).neq("sender_id", user.id).execute()
    return {"message": "メッセージを既読にしました"}

# --- 管理者用（簡易） ---

@app.put("/admin/verify/{user_id}", summary="【運営用】ユーザーの承認", tags=["管理"])
async def verify_student(user_id: str, admin_user=Depends(get_current_user)):
    supabase.table("profiles").update({"is_verified": True}).eq("id", user_id).execute()
    return {"message": "ユーザーを承認済みステータスに変更しました"}