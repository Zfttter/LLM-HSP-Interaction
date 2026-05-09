from pydantic import BaseModel


class SurveySubmission(BaseModel):
    # HSPS-18 (keys: hsps_1 .. hsps_18, values: 1-7)
    hsps_1: int
    hsps_2: int
    hsps_3: int
    hsps_4: int
    hsps_5: int
    hsps_6: int
    hsps_7: int
    hsps_8: int
    hsps_9: int
    hsps_10: int
    hsps_11: int
    hsps_12: int
    hsps_13: int
    hsps_14: int
    hsps_15: int
    hsps_16: int
    hsps_17: int
    hsps_18: int
    # BFI-10 (keys: bfi_1 .. bfi_10, values: 1-5)
    bfi_1: int
    bfi_2: int
    bfi_3: int
    bfi_4: int
    bfi_5: int
    bfi_6: int
    bfi_7: int
    bfi_8: int
    bfi_9: int
    bfi_10: int
    # Demographics
    age: int
    gender: str
    native_english: str   # "yes" / "no"
    ai_usage: str         # never/rarely/sometimes/often/very_often
    country: str


class IntroSubmission(BaseModel):
    message: str


class ChatMessage(BaseModel):
    message: str


class PostSurveySubmission(BaseModel):
    # Section A
    general_empathy:      int          # 1-7
    satisfaction:         int          # 1-7
    trust:                int          # 1-7
    conversation_quality: int          # 1-7
    # Section B
    affective_empathy_1:  int          # 1-7
    affective_empathy_2:  int          # 1-7
    cognitive_empathy:    int          # 1-7
    associative_empathy:      int       # 1-7
    emotional_responsiveness: int       # 1-7
    empathic_accuracy:        int       # 1-7
    implicit_understanding:   int       # 1-7
    # Section C
    closeness_ios:        int          # 1-7
    emotional_relief:     int          # 1-7
    # Section D
    perceived_sycophancy: int          # 1-7
    # Bonus
    mbti_guess:           str
