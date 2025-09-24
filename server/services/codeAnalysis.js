const esprima = require('esprima');
const esquery = require('esquery');
const acorn = require('acorn');
const walk = require('acorn-walk');

class CodeAnalysisService {
  constructor() {
    this.supportedLanguages = {
      '.js': 'javascript',
      '.jsx': 'javascript',
      '.ts': 'typescript',
      '.tsx': 'typescript',
      '.mjs': 'javascript'
    };
  }

  /**
   * Main analysis function that processes code and returns comprehensive analysis
   */
  async analyzeCode(content, filename, language = 'javascript') {
    try {
      const analysis = {
        ast: null,
        complexity: {},
        security: {},
        dependencies: {},
        cloudServices: [],
        metrics: {}
      };

      if (language === 'javascript' || language === 'typescript') {
        analysis.ast = this.parseJavaScript(content);
        analysis.complexity = this.calculateComplexity(analysis.ast, content);
        analysis.security = this.analyzeSecurity(analysis.ast, content);
        analysis.dependencies = this.extractDependencies(analysis.ast, content);
        analysis.cloudServices = this.detectCloudServices(analysis.ast, content);
        analysis.metrics = this.calculateMetrics(content);
      }

      return analysis;
    } catch (error) {
      console.error('Code analysis error:', error);
      throw new Error(`Analysis failed: ${error.message}`);
    }
  }

  /**
   * Parse JavaScript/TypeScript code into AST
   */
  parseJavaScript(content) {
    try {
      return esprima.parseScript(content, {
        ecmaVersion: 2022,
        sourceType: 'module',
        locations: true,
        comments: true,
        attachComments: true
      });
    } catch (error) {
      // Fallback to module parsing
      try {
        return esprima.parseModule(content, {
          ecmaVersion: 2022,
          locations: true,
          comments: true,
          attachComments: true
        });
      } catch (moduleError) {
        throw new Error(`Parse error: ${error.message}`);
      }
    }
  }

  /**
   * Calculate cyclomatic complexity and maintainability metrics
   */
  calculateComplexity(ast, content) {
    const complexity = {
      cyclomatic: 1,
      maintainabilityIndex: 0,
      halsteadVolume: 0,
      functionComplexity: []
    };

    try {
      // Calculate cyclomatic complexity
      const complexityNodes = esquery(ast, [
        'IfStatement',
        'WhileStatement', 
        'ForStatement',
        'ForInStatement',
        'ForOfStatement',
        'SwitchCase',
        'ConditionalExpression',
        'LogicalExpression[operator="||"]',
        'LogicalExpression[operator="&&"]',
        'CatchClause'
      ].join(','));
      
      complexity.cyclomatic += complexityNodes.length;

      // Analyze individual functions
      const functions = esquery(ast, 'FunctionDeclaration, FunctionExpression, ArrowFunctionExpression');
      
      functions.forEach(func => {
        const funcComplexity = this.calculateFunctionComplexity(func);
        complexity.functionComplexity.push({
          name: func.id ? func.id.name : 'anonymous',
          line: func.loc ? func.loc.start.line : 0,
          complexity: funcComplexity,
          parameters: func.params.map(p => p.name || p.type),
          returnType: this.inferReturnType(func)
        });
      });

      // Calculate Halstead volume (simplified)
      const operators = content.match(/[+\-*/=<>!&|%^~?:;,.(){}\[\]]/g) || [];
      const operands = content.match(/[a-zA-Z_$][a-zA-Z0-9_$]*/g) || [];
      const uniqueOperators = [...new Set(operators)].length;
      const uniqueOperands = [...new Set(operands)].length;
      const totalLength = operators.length + operands.length;
      const vocabulary = uniqueOperators + uniqueOperands;
      
      complexity.halsteadVolume = vocabulary > 0 ? totalLength * Math.log2(vocabulary) : 0;

      // Calculate maintainability index
      const linesOfCode = content.split('\n').length;
      complexity.maintainabilityIndex = Math.max(0, 
        171 - 5.2 * Math.log(complexity.halsteadVolume) - 
        0.23 * complexity.cyclomatic - 16.2 * Math.log(linesOfCode)
      );

      return complexity;
    } catch (error) {
      console.error('Complexity calculation error:', error);
      return complexity;
    }
  }

  /**
   * Calculate complexity for individual function
   */
  calculateFunctionComplexity(funcNode) {
    let complexity = 1;
    
    const traverse = (node) => {
      if (!node) return;
      
      switch (node.type) {
        case 'IfStatement':
        case 'ConditionalExpression':
        case 'WhileStatement':
        case 'ForStatement':
        case 'ForInStatement':
        case 'ForOfStatement':
        case 'SwitchCase':
        case 'CatchClause':
          complexity++;
          break;
        case 'LogicalExpression':
          if (node.operator === '&&' || node.operator === '||') {
            complexity++;
          }
          break;
      }
      
      // Recursively traverse child nodes
      for (const key in node) {
        if (node[key] && typeof node[key] === 'object') {
          if (Array.isArray(node[key])) {
            node[key].forEach(traverse);
          } else if (node[key].type) {
            traverse(node[key]);
          }
        }
      }
    };
    
    traverse(funcNode.body);
    return complexity;
  }

  /**
   * Analyze security vulnerabilities in code
   */
  analyzeSecurity(ast, content) {
    const security = {
      score: 5, // Start with perfect score
      issues: []
    };

    try {
      // Check for eval usage
      const evalCalls = esquery(ast, 'CallExpression[callee.name="eval"]');
      evalCalls.forEach(call => {
        security.issues.push({
          type: 'eval-usage',
          severity: 'critical',
          line: call.loc ? call.loc.start.line : 0,
          description: 'Use of eval() can lead to code injection vulnerabilities'
        });
        security.score -= 2;
      });

      // Check for innerHTML usage
      const innerHTMLUsage = esquery(ast, 'MemberExpression[property.name="innerHTML"]');
      innerHTMLUsage.forEach(usage => {
        security.issues.push({
          type: 'innerHTML-usage',
          severity: 'medium',
          line: usage.loc ? usage.loc.start.line : 0,
          description: 'Direct innerHTML manipulation can lead to XSS vulnerabilities'
        });
        security.score -= 0.5;
      });

      // Check for hardcoded credentials
      const strings = esquery(ast, 'Literal[raw*="password"], Literal[raw*="secret"], Literal[raw*="key"]');
      strings.forEach(str => {
        if (str.value && typeof str.value === 'string' && str.value.length > 8) {
          security.issues.push({
            type: 'hardcoded-credential',
            severity: 'high',
            line: str.loc ? str.loc.start.line : 0,
            description: 'Potential hardcoded credential detected'
          });
          security.score -= 1;
        }
      });

      // Check for unsafe regex patterns
      const regexNodes = esquery(ast, 'NewExpression[callee.name="RegExp"], Literal[regex]');
      regexNodes.forEach(regex => {
        const pattern = regex.regex ? regex.regex.pattern : regex.arguments?.[0]?.value;
        if (pattern && this.isUnsafeRegex(pattern)) {
          security.issues.push({
            type: 'unsafe-regex',
            severity: 'medium',
            line: regex.loc ? regex.loc.start.line : 0,
            description: 'Potentially vulnerable regular expression pattern'
          });
          security.score -= 0.5;
        }
      });

      security.score = Math.max(0, Math.min(5, security.score));
      return security;
    } catch (error) {
      console.error('Security analysis error:', error);
      return security;
    }
  }

  /**
   * Check if regex pattern is potentially unsafe (ReDoS)
   */
  isUnsafeRegex(pattern) {
    // Simple heuristic for ReDoS detection
    const dangerousPatterns = [
      /(\w+)+/,  // Nested quantifiers
      /(\w*)*/, 
      /(\w+)*\w+/,
      /(a+)+$/,
      /(.*)*$/
    ];
    
    return dangerousPatterns.some(dangerous => dangerous.test(pattern));
  }

  /**
   * Extract dependencies, imports, exports, functions, variables
   */
  extractDependencies(ast, content) {
    const dependencies = {
      imports: [],
      exports: [],
      functions: [],
      variables: [],
      apiCalls: [],
      databaseOps: []
    };

    try {
      // Extract imports
      const importNodes = esquery(ast, 'ImportDeclaration');
      importNodes.forEach(imp => {
        dependencies.imports.push(imp.source.value);
      });

      // Extract require calls
      const requireCalls = esquery(ast, 'CallExpression[callee.name="require"]');
      requireCalls.forEach(req => {
        if (req.arguments[0] && req.arguments[0].type === 'Literal') {
          dependencies.imports.push(req.arguments[0].value);
        }
      });

      // Extract exports
      const exportNodes = esquery(ast, 'ExportNamedDeclaration, ExportDefaultDeclaration');
      exportNodes.forEach(exp => {
        if (exp.declaration) {
          if (exp.declaration.id) {
            dependencies.exports.push(exp.declaration.id.name);
          }
        }
      });

      // Extract functions (already done in complexity analysis)
      const functions = esquery(ast, 'FunctionDeclaration, FunctionExpression, ArrowFunctionExpression');
      functions.forEach(func => {
        if (func.id) {
          dependencies.functions.push({
            name: func.id.name,
            line: func.loc ? func.loc.start.line : 0,
            complexity: this.calculateFunctionComplexity(func),
            parameters: func.params.map(p => p.name || p.type || 'unknown'),
            returnType: this.inferReturnType(func)
          });
        }
      });

      // Extract variable declarations
      const variables = esquery(ast, 'VariableDeclarator');
      variables.forEach(varDecl => {
        if (varDecl.id && varDecl.id.name) {
          dependencies.variables.push({
            name: varDecl.id.name,
            type: this.inferVariableType(varDecl),
            scope: this.determineScope(varDecl),
            line: varDecl.loc ? varDecl.loc.start.line : 0
          });
        }
      });

      // Extract API calls
      const apiCalls = esquery(ast, 'CallExpression[callee.name="fetch"], CallExpression[callee.object.name="axios"]');
      apiCalls.forEach(call => {
        const endpoint = this.extractEndpoint(call);
        const method = this.extractHttpMethod(call);
        
        dependencies.apiCalls.push({
          type: call.callee.name === 'fetch' ? 'fetch' : 'axios',
          endpoint: endpoint,
          method: method,
          line: call.loc ? call.loc.start.line : 0
        });
      });

      // Extract database operations
      dependencies.databaseOps = this.extractDatabaseOps(ast);

      return dependencies;
    } catch (error) {
      console.error('Dependency extraction error:', error);
      return dependencies;
    }
  }

  /**
   * Detect cloud service usage
   */
  detectCloudServices(ast, content) {
    const cloudServices = [];
    
    try {
      // AWS SDK detection
      const awsImports = esquery(ast, 'ImportDeclaration[source.value*="aws-sdk"], CallExpression[callee.name="require"][arguments.0.value*="aws-sdk"]');
      awsImports.forEach(imp => {
        cloudServices.push({
          provider: 'AWS',
          service: 'AWS SDK',
          usage: 'General AWS services',
          line: imp.loc ? imp.loc.start.line : 0
        });
      });

      // Firebase detection
      const firebaseImports = esquery(ast, 'ImportDeclaration[source.value*="firebase"], CallExpression[callee.name="require"][arguments.0.value*="firebase"]');
      firebaseImports.forEach(imp => {
        cloudServices.push({
          provider: 'Google Cloud',
          service: 'Firebase',
          usage: 'Firebase services',
          line: imp.loc ? imp.loc.start.line : 0
        });
      });

      // Supabase detection
      const supabaseImports = esquery(ast, 'ImportDeclaration[source.value*="supabase"], CallExpression[callee.name="require"][arguments.0.value*="supabase"]');
      supabaseImports.forEach(imp => {
        cloudServices.push({
          provider: 'Supabase',
          service: 'Supabase Client',
          usage: 'Database and auth services',
          line: imp.loc ? imp.loc.start.line : 0
        });
      });

      return cloudServices;
    } catch (error) {
      console.error('Cloud service detection error:', error);
      return cloudServices;
    }
  }

  /**
   * Calculate basic code metrics
   */
  calculateMetrics(content) {
    const lines = content.split('\n');
    const nonEmptyLines = lines.filter(line => line.trim().length > 0);
    const commentLines = lines.filter(line => line.trim().startsWith('//') || line.trim().startsWith('*'));
    
    return {
      totalLines: lines.length,
      codeLines: nonEmptyLines.length,
      commentLines: commentLines.length,
      emptyLines: lines.length - nonEmptyLines.length,
      characters: content.length,
      words: content.split(/\s+/).filter(word => word.length > 0).length
    };
  }

  // Helper methods
  inferReturnType(func) {
    // Basic return type inference
    if (func.body && func.body.body) {
      const returnStatements = esquery(func, 'ReturnStatement');
      if (returnStatements.length > 0) {
        return 'mixed'; // Simplified
      }
    }
    return 'void';
  }

  inferVariableType(varDecl) {
    if (varDecl.init) {
      switch (varDecl.init.type) {
        case 'Literal':
          return typeof varDecl.init.value;
        case 'ArrayExpression':
          return 'array';
        case 'ObjectExpression':
          return 'object';
        case 'FunctionExpression':
        case 'ArrowFunctionExpression':
          return 'function';
        default:
          return 'unknown';
      }
    }
    return 'undefined';
  }

  determineScope(node) {
    // Simplified scope determination
    let parent = node.parent;
    while (parent) {
      if (parent.type === 'FunctionDeclaration' || parent.type === 'FunctionExpression') {
        return 'function';
      }
      if (parent.type === 'BlockStatement') {
        return 'block';
      }
      parent = parent.parent;
    }
    return 'global';
  }

  extractEndpoint(callNode) {
    if (callNode.arguments && callNode.arguments[0]) {
      if (callNode.arguments[0].type === 'Literal') {
        return callNode.arguments[0].value;
      }
      if (callNode.arguments[0].type === 'TemplateLiteral') {
        return 'template_literal';
      }
    }
    return 'dynamic';
  }

  extractHttpMethod(callNode) {
    if (callNode.callee.name === 'fetch') {
      if (callNode.arguments[1] && callNode.arguments[1].type === 'ObjectExpression') {
        const methodProp = callNode.arguments[1].properties.find(p => 
          p.key && p.key.name === 'method'
        );
        if (methodProp && methodProp.value && methodProp.value.type === 'Literal') {
          return methodProp.value.value;
        }
      }
      return 'GET';
    }
    return 'unknown';
  }

  extractDatabaseOps(ast) {
    const dbOps = [];
    
    // MongoDB operations
    const mongoOps = esquery(ast, 'CallExpression[callee.property.name=/find|findOne|insert|update|delete|aggregate/]');
    mongoOps.forEach(op => {
      dbOps.push({
        type: 'MongoDB',
        operation: op.callee.property.name,
        table: 'collection',
        line: op.loc ? op.loc.start.line : 0
      });
    });

    // SQL-like operations (simplified detection)
    const sqlPatterns = ['SELECT', 'INSERT', 'UPDATE', 'DELETE', 'CREATE', 'DROP'];
    const literals = esquery(ast, 'Literal[value*="SELECT"], Literal[value*="INSERT"], Literal[value*="UPDATE"], Literal[value*="DELETE"]');
    literals.forEach(literal => {
      const query = literal.value.toUpperCase();
      const operation = sqlPatterns.find(pattern => query.includes(pattern));
      if (operation) {
        dbOps.push({
          type: 'SQL',
          operation: operation,
          table: 'unknown',
          line: literal.loc ? literal.loc.start.line : 0
        });
      }
    });

    return dbOps;
  }
}

module.exports = new CodeAnalysisService();