from pydub import AudioSegment
from pydub.effects import normalize

def apply_great_sage_effect(audio_path):
    sound = AudioSegment.from_file(audio_path)
    
    # 1. Pitch-shift slightly lower / robotic adjustment
    # 2. Add subtle delay/echo effect (simulating the World Voice resonance)
    echo = sound.delay(30).minus(6)
    sage_voice = sound.overlay(echo)
    
    # 3. Slightly flatten dynamic range
    sage_voice = normalize(sage_voice)
    sage_voice.export("great_sage_output.mp3", format="mp3")