const mongoose = require('mongoose');

const aiSessionSchema = new mongoose.Schema({
  userId: {
    type: mongoose.Schema.Types.ObjectId,
    ref: 'User',
    required: true
  },
  projectId: {
    type: mongoose.Schema.Types.ObjectId,
    ref: 'Project',
    required: true
  },
  provider: {
    type: String,
    enum: ['openai', 'anthropic', 'google', 'custom'],
    required: true
  },
  model: {
    type: String,
    required: true
  },
  prompt: {
    type: String,
    required: true
  },
  response: {
    type: String,
    required: true
  },
  context: {
    intent: String,
    constraints: [String],
    expectedChanges: String
  },
  codeGenerated: {
    type: String,
    default: ''
  },
  filesBefore: [{
    fileId: mongoose.Schema.Types.ObjectId,
    content: String
  }],
  filesAfter: [{
    fileId: mongoose.Schema.Types.ObjectId,
    content: String
  }],
  score: {
    followedInstructions: Number,
    codeQuality: Number,
    matchedIntent: Number,
    overall: Number
  },
  createdAt: {
    type: Date,
    default: Date.now
  }
});

module.exports = mongoose.model('AISession', aiSessionSchema);