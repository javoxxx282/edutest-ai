from dataclasses import dataclass, field


@dataclass
class QuizSession:
    questions: list[dict] = field(default_factory=list)
    current_index: int = 0
    score: int = 0
    wrong_answers: list[dict] = field(default_factory=list)

    @property
    def total(self) -> int:
        return len(self.questions)

    @property
    def is_finished(self) -> bool:
        return self.current_index >= self.total

    def current_question(self) -> dict | None:
        if self.is_finished:
            return None
        return self.questions[self.current_index]

    def answer(self, chosen: str) -> bool:
        q = self.current_question()
        correct = q["correct"]
        is_correct = chosen == correct
        if is_correct:
            self.score += 1
        else:
            self.wrong_answers.append({
                "question": q["question"],
                "chosen": chosen,
                "chosen_text": q["options"].get(chosen, ""),
                "correct": correct,
                "correct_text": q["options"].get(correct, ""),
                "explanation": q.get("explanation", ""),
            })
        self.current_index += 1
        return is_correct
