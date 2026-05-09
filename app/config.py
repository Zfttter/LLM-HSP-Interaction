from functools import lru_cache
from pydantic_settings import BaseSettings


# ── Survey instruments ────────────────────────────────────────────────────────

HSPS_ITEMS = [
    "Are you easily overwhelmed by strong sensory input?",
    "Do you seem to be aware of subtleties in your environment?",
    "Other people's moods affect you?",
    "Do you tend to be more sensitive to pain?",
    "Do you find yourself needing to withdraw during busy days, into a darkened room or a place where you can have some privacy and relief from stimulation?",
    "Are you particularly sensitive to the effects of caffeine?",
    "Are you easily overwhelmed by things like bright lights, strong smells, coarse fabrics, or sirens close by?",
    "Do you have a rich, complex inner life?",
    "Are you made uncomfortable by loud noises?",
    "Are you deeply moved by the arts or music?",
    "Does your nervous system sometimes feel so frazzled that you just need to go off by yourself?",
    "Are you conscientious?",
    "Do you startle easily?",
    "Do you get rattled when you have a lot to do in a short amount of time?",
    "When people are uncomfortable in a physical environment, do you tend to know what needs to be done to make it more comfortable (e.g., changing the lighting or the seating)?",
    "Are you annoyed when people try to get you to do too many things at once?",
    "Do you try hard to avoid making mistakes or forgetting things?",
    "Do you make a point to avoid violent movies or TV shows?",
]

BFI_ITEMS = [
    ("is talkative",                                "E+"),   # 1
    ("tends to find fault with others",             "A-"),   # 2
    ("does a thorough job",                         "C+"),   # 3
    ("is depressed, blue",                          "N+"),   # 4
    ("is original, comes up with new ideas",        "O+"),   # 5
    ("is reserved",                                 "E-"),   # 6
    ("is helpful and unselfish with others",        "A+"),   # 7
    ("can be somewhat careless",                    "C-"),   # 8
    ("is relaxed, handles stress well",             "N-"),   # 9
    ("is curious about many different things",      "O+"),   # 10
    ("is full of energy",                           "E+"),   # 11
    ("starts quarrels with others",                 "A-"),   # 12
    ("is a reliable worker",                        "C+"),   # 13
    ("can be tense",                                "N+"),   # 14
    ("is ingenious, a deep thinker",                "O+"),   # 15
    ("generates a lot of enthusiasm",               "E+"),   # 16
    ("has a forgiving nature",                      "A+"),   # 17
    ("tends to be disorganized",                    "C-"),   # 18
    ("worries a lot",                               "N+"),   # 19
    ("has an active imagination",                   "O+"),   # 20
    ("tends to be quiet",                           "E-"),   # 21
    ("is generally trusting",                       "A+"),   # 22
    ("tends to be lazy",                            "C-"),   # 23
    ("is emotionally stable, not easily upset",     "N-"),   # 24
    ("is inventive",                                "O+"),   # 25
    ("has an assertive personality",                "E+"),   # 26
    ("can be cold and aloof",                       "A-"),   # 27
    ("perseveres until the task is finished",       "C+"),   # 28
    ("can be moody",                                "N+"),   # 29
    ("values artistic, aesthetic experiences",      "O+"),   # 30
    ("is sometimes shy, inhibited",                 "E-"),   # 31
    ("is considerate and kind to almost everyone",  "A+"),   # 32
    ("does things efficiently",                     "C+"),   # 33
    ("remains calm in tense situations",            "N-"),   # 34
    ("prefers work that is routine",                "O-"),   # 35
    ("is outgoing, sociable",                       "E+"),   # 36
    ("is sometimes rude to others",                 "A-"),   # 37
    ("makes plans and follows through with them",   "C+"),   # 38
    ("gets nervous easily",                         "N+"),   # 39
    ("likes to reflect, play with ideas",           "O+"),   # 40
    ("has few artistic interests",                  "O-"),   # 41
    ("likes to cooperate with others",              "A+"),   # 42
    ("is easily distracted",                        "C-"),   # 43
    ("is sophisticated in art, music, or literature", "O+"), # 44
]

HSPS_LABELS = {1: "Not at all", 7: "Extremely"}
BFI_LABELS = {1: "Disagree strongly", 5: "Agree strongly"}
BFI_SCALE_LABELS = ["", "Disagree strongly", "Disagree a little",
                    "Neither agree nor disagree", "Agree a little", "Agree strongly"]
POST_SURVEY_LABELS = {1: "Strongly disagree", 7: "Strongly agree"}

# ── Condition definitions ─────────────────────────────────────────────────────

PLATFORMS = [
    "gpt-4o",
    "gpt-4o-mini",
    "claude-sonnet-4-6",
    "gemini-2.0-flash",
    "deepseek-chat",
    "llama-3.3-70b-versatile",
]

PLATFORM_DISPLAY = {
    "gpt-4o": "GPT-4o",
    "gpt-4o-mini": "GPT-4o Mini",
    "claude-sonnet-4-6": "Claude Sonnet",
    "gemini-2.0-flash": "Gemini 2.0 Flash",
    "deepseek-chat": "DeepSeek Chat",
    "llama-3.3-70b-versatile": "Llama 3.3 70B",
}

TOPICS = ["social_anxiety", "rumination", "anticipatory_anxiety"]

TOPIC_DISPLAY = {
    "social_anxiety": "Social Anxiety",
    "rumination": "Rumination",
    "anticipatory_anxiety": "Anticipatory Anxiety",
}

TOPIC_PROMPTS = {
    "social_anxiety": (
        "Think of a time when you felt socially anxious or uncomfortable around others — "
        "a moment that has stayed with you. This could be a time when you were criticized or "
        "embarrassed in front of others, a situation where you needed to express frustration or "
        "displeasure but couldn't find the words, or a moment when you had to speak in front of "
        "an authority figure or a group and felt exposed.\n\n"
        "Choose whichever feels most real to you, or think of a similar experience of your own.\n\n"
        "Share what happened — who was there, and what was it about the moment that made it so "
        "hard? What were you thinking while it was happening? Was there something you wished you "
        "could have said or done differently?"
    ),
    "rumination": (
        "Think of something from your past that you find yourself returning to more than once — "
        "a moment you can't quite let go of. This could be something you said that you later "
        "regretted, a decision you've second-guessed, a conflict that never fully resolved, a time "
        "you felt misunderstood or left out, or the end of a friendship or relationship that "
        "you've kept replaying in your mind.\n\n"
        "Choose whichever feels most relevant to you, or think of a similar experience of your own.\n\n"
        "Tell me about it — what happened, and why do you think it keeps coming back to you? "
        "When it resurfaces, what feeling comes with it? Is there something about it that still "
        "feels unresolved?"
    ),
    "anticipatory_anxiety": (
        "Think of something coming up in your life that you've been feeling anxious or uncertain "
        "about — something whose outcome feels unclear or out of your control. This could be an "
        "upcoming interview, audition, or performance, waiting for an important result, a difficult "
        "conversation you know you need to have, uncertainty about a major life decision, or "
        "concern about how an important relationship might unfold.\n\n"
        "Choose whichever feels most relevant to you, or think of a similar experience of your own.\n\n"
        "Tell me what's ahead — what is it, and what's the part that worries you most? When you "
        "imagine it going wrong, what does that look like? Is there something specific you're "
        "afraid of, or is it more of a general sense of dread you can't quite name?"
    ),
}

# ── System prompt ─────────────────────────────────────────────────────────────

SYSTEM_PROMPT = (
    "You are a conversational AI assistant. The user will share "
    "a personal experience with you. Listen to what they say, "
    "respond to their message, and ask a follow-up question "
    "to help them continue sharing."
)

LLM_TEMPERATURE = 0.7
LLM_MAX_TOKENS = 500
CONVERSATION_ROUNDS = 5

AI_NAME = "Sage"

# ── Voice pipeline constants ───────────────────────────────────────────────────

TTS_VOICE     = "nova"
TTS_MODEL     = "tts-1"
WHISPER_MODEL = "whisper-1"

INTRO_TURNS = 1
STORY_TURNS = 5
MAX_TURNS   = INTRO_TURNS + STORY_TURNS   # 6 total

TOPIC_DESCRIPTIONS = {
    "social_anxiety":       "a time when you felt socially anxious or uncomfortable around others",
    "rumination":           "something from your past that you find yourself returning to more than once",
    "anticipatory_anxiety": "something coming up in your life that you've been feeling anxious or uncertain about",
}

OPENING_MESSAGE = (
    "Hi there! I'm really glad you're here. Before we dive in, I'd love to know "
    "a little about you — could you tell me your name, and maybe one thing you enjoy doing?"
)


def build_system_prompt(topic: str, turn_number: int) -> str:
    desc = TOPIC_DESCRIPTIONS.get(topic, topic)
    intro = f"""You are {AI_NAME}, a warm and empathic AI conversation partner taking part in a research study.\n\nThe participant has been asked to share about: {desc}\n"""
    if turn_number <= 1:
        phase = f"""\nCURRENT PHASE — Introduction (turn {turn_number}):\nYou have just asked the participant their name and one thing they enjoy. Respond warmly to what they share, then immediately transition into inviting them to share about the topic. For example: "That's lovely to hear. I'd love to hear about {desc} — whenever you're ready, please take your time and share what comes to mind."\n"""
    elif turn_number >= MAX_TURNS:
        phase = f"""\nCURRENT PHASE — Closing (final turn):\nThis is the last turn of the conversation. Do NOT ask any follow-up questions. Instead, give a warm, genuine closing response: acknowledge what the participant has shared, offer a brief empathic reflection, and say a kind farewell. Make them feel heard and appreciated.\n"""
    else:
        phase = f"""\nCURRENT PHASE — Story sharing (turn {turn_number}):\nThe participant is sharing their personal experience. Listen with empathy, reflect back what they've shared,\nand ask exactly one thoughtful follow-up question to help them continue opening up.\n"""
    rules = """\nVOICE CONVERSATION RULES (strictly follow):\n- Keep every response to 2–4 sentences maximum.\n- Do NOT use bullet points, numbered lists, headers, or any markdown formatting.\n- Speak naturally and warmly, exactly as you would in a real face-to-face conversation.\n- Never mention that you are an AI or part of a study unless the participant directly asks.\n"""
    return intro + phase + rules

# ── App settings ──────────────────────────────────────────────────────────────

class Settings(BaseSettings):
    SECRET_KEY: str = "change-this-to-a-long-random-string-in-production"
    ADMIN_KEY: str = "admin"
    OPENAI_API_KEY: str = ""
    ANTHROPIC_API_KEY: str = ""
    GOOGLE_API_KEY: str = ""
    DEEPSEEK_API_KEY: str = ""
    GROQ_API_KEY: str = ""
    SUPABASE_URL: str = ""
    SUPABASE_KEY: str = ""

    class Config:
        env_file = ".env"


@lru_cache()
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
