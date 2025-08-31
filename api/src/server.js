import express from "express";
const app = express();
app.get("/health", (_req, res) => res.json({ status: "ok" }));
app.listen(8080, () => console.log("API up on :8080"));
