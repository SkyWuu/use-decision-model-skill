#!/usr/bin/env bash
# Fixture: billing-crud-java — negative control. A Java invoice service with only CRUD,
# money arithmetic, billing-period date math, an enum state machine, an exact currency lookup
# and role checks. The correct answer is "no suitable decision-model opportunity".
# There are fix commits, but all of them are on arithmetic and dates.
# Usage: build.sh <out-dir>
set -euo pipefail
. "$(cd "$(dirname "$0")/.." && pwd)/_lib.sh"
fixture_init "$@"

ZHOU="Zhou Bin <zhoubin@example.com>"
WU="Wu Lan <wulan@example.com>"

PKG=src/main/java/com/example/billing
TPKG=src/test/java/com/example/billing
mkdir -p $PKG/{controller,service,security,model,repository,export} $TPKG

# ---------------------------------------------------------------- 1. skeleton
cat > README.md <<'EOF'
# billing-service

企业客户账单服务：发票的增删改查、金额计算（税额、折扣、四舍五入）、账期计算、逾期判断、CSV 导出和权限检查。

- 接口：`InvoiceController`（REST）。
- 金额统一用 `BigDecimal`，保留两位小数，四舍五入。
- 只有财务角色可以作废发票。
EOF

cat > pom.xml <<'EOF'
<project xmlns="http://maven.apache.org/POM/4.0.0">
  <modelVersion>4.0.0</modelVersion>
  <groupId>com.example</groupId>
  <artifactId>billing-service</artifactId>
  <version>1.4.0</version>
  <properties>
    <maven.compiler.release>21</maven.compiler.release>
  </properties>
  <dependencies>
    <dependency>
      <groupId>org.springframework.boot</groupId>
      <artifactId>spring-boot-starter-web</artifactId>
      <version>3.5.0</version>
    </dependency>
    <dependency>
      <groupId>org.junit.jupiter</groupId>
      <artifactId>junit-jupiter</artifactId>
      <version>5.12.0</version>
      <scope>test</scope>
    </dependency>
  </dependencies>
</project>
EOF

cat > $PKG/BillingApplication.java <<'EOF'
package com.example.billing;

import org.springframework.boot.SpringApplication;
import org.springframework.boot.autoconfigure.SpringBootApplication;

@SpringBootApplication
public class BillingApplication {
    public static void main(String[] args) {
        SpringApplication.run(BillingApplication.class, args);
    }
}
EOF

cat > $PKG/model/InvoiceStatus.java <<'EOF'
package com.example.billing.model;

import java.util.EnumSet;
import java.util.Set;

public enum InvoiceStatus {
    DRAFT, ISSUED, PAID, OVERDUE, VOID;

    /** 允许的状态流转。 */
    public Set<InvoiceStatus> next() {
        return switch (this) {
            case DRAFT -> EnumSet.of(ISSUED, VOID);
            case ISSUED -> EnumSet.of(PAID, OVERDUE, VOID);
            case OVERDUE -> EnumSet.of(PAID, VOID);
            case PAID, VOID -> EnumSet.noneOf(InvoiceStatus.class);
        };
    }
}
EOF

cat > $PKG/model/Invoice.java <<'EOF'
package com.example.billing.model;

import java.math.BigDecimal;
import java.time.LocalDate;

public class Invoice {
    public Long id;
    public String customerId;
    public String currency;
    public BigDecimal netAmount;
    public BigDecimal taxRate;
    public BigDecimal discount;
    public LocalDate periodStart;
    public LocalDate periodEnd;
    public LocalDate dueDate;
    public InvoiceStatus status = InvoiceStatus.DRAFT;
}
EOF

cat > $PKG/repository/InvoiceRepository.java <<'EOF'
package com.example.billing.repository;

import com.example.billing.model.Invoice;
import java.util.List;
import java.util.Optional;

public interface InvoiceRepository {
    Optional<Invoice> findById(Long id);
    List<Invoice> findByCustomer(String customerId);
    Invoice save(Invoice invoice);
    void delete(Long id);
}
EOF

cat > $PKG/service/InvoiceService.java <<'EOF'
package com.example.billing.service;

import com.example.billing.model.Invoice;
import java.math.BigDecimal;
import java.math.RoundingMode;
import java.util.Map;

public class InvoiceService {
    private static final Map<String, String> CURRENCY_ALIASES = Map.of(
        "人民币", "CNY", "RMB", "CNY", "CNY", "CNY",
        "美元", "USD", "USD", "USD");

    /** 含税金额 = (净额 - 折扣) × (1 + 税率)，保留两位小数。 */
    public BigDecimal grossAmount(Invoice invoice) {
        BigDecimal base = invoice.netAmount.subtract(invoice.discount);
        return base.multiply(BigDecimal.ONE.add(invoice.taxRate)).setScale(2, RoundingMode.HALF_UP);
    }

    public String normalizeCurrency(String raw) {
        String code = CURRENCY_ALIASES.get(raw.trim().toUpperCase());
        if (code == null) {
            code = CURRENCY_ALIASES.get(raw.trim());
        }
        if (code == null) {
            throw new IllegalArgumentException("unknown currency: " + raw);
        }
        return code;
    }
}
EOF

cat > $PKG/security/PermissionChecker.java <<'EOF'
package com.example.billing.security;

import java.util.Set;

public class PermissionChecker {
    public enum Role { VIEWER, SALES, FINANCE, ADMIN }

    private static final Set<Role> CAN_VOID = Set.of(Role.FINANCE, Role.ADMIN);
    private static final Set<Role> CAN_EDIT = Set.of(Role.SALES, Role.FINANCE, Role.ADMIN);

    public boolean canVoid(Role role) {
        return CAN_VOID.contains(role);
    }

    public boolean canEdit(Role role) {
        return CAN_EDIT.contains(role);
    }
}
EOF

cat > $PKG/controller/InvoiceController.java <<'EOF'
package com.example.billing.controller;

import com.example.billing.model.Invoice;
import com.example.billing.model.InvoiceStatus;
import com.example.billing.repository.InvoiceRepository;
import com.example.billing.security.PermissionChecker;
import com.example.billing.security.PermissionChecker.Role;
import java.util.List;
import org.springframework.http.ResponseEntity;
import org.springframework.web.bind.annotation.*;

@RestController
@RequestMapping("/api/invoices")
public class InvoiceController {
    private final InvoiceRepository repo;
    private final PermissionChecker permissions = new PermissionChecker();

    public InvoiceController(InvoiceRepository repo) {
        this.repo = repo;
    }

    @GetMapping("/{id}")
    public ResponseEntity<Invoice> get(@PathVariable Long id) {
        return repo.findById(id).map(ResponseEntity::ok).orElse(ResponseEntity.notFound().build());
    }

    @GetMapping
    public List<Invoice> list(@RequestParam String customerId) {
        return repo.findByCustomer(customerId);
    }

    @PostMapping
    public Invoice create(@RequestBody Invoice invoice, @RequestHeader("X-Role") Role role) {
        if (!permissions.canEdit(role)) throw new SecurityException("forbidden");
        return repo.save(invoice);
    }

    @PostMapping("/{id}/void")
    public ResponseEntity<Invoice> voidInvoice(@PathVariable Long id, @RequestHeader("X-Role") Role role) {
        if (!permissions.canVoid(role)) return ResponseEntity.status(403).build();
        Invoice invoice = repo.findById(id).orElseThrow();
        if (!invoice.status.next().contains(InvoiceStatus.VOID)) return ResponseEntity.badRequest().build();
        invoice.status = InvoiceStatus.VOID;
        return ResponseEntity.ok(repo.save(invoice));
    }

    @DeleteMapping("/{id}")
    public ResponseEntity<Void> delete(@PathVariable Long id, @RequestHeader("X-Role") Role role) {
        if (!permissions.canVoid(role)) return ResponseEntity.status(403).build();
        repo.delete(id);
        return ResponseEntity.noContent().build();
    }
}
EOF
commit "$ZHOU" "2026-02-10T10:00:00+08:00" "init: 发票增删改查、金额计算和权限检查"

# ---------------------------------------------------------------- 2. billing period
cat > $PKG/service/BillingPeriodService.java <<'EOF'
package com.example.billing.service;

import com.example.billing.model.Invoice;
import java.time.LocalDate;
import java.time.temporal.TemporalAdjusters;

public class BillingPeriodService {
    public static final int PAYMENT_TERM_DAYS = 30;

    /** 自然月账期：所在月的第一天到最后一天。 */
    public void assignPeriod(Invoice invoice, LocalDate anyDayInMonth) {
        invoice.periodStart = anyDayInMonth.with(TemporalAdjusters.firstDayOfMonth());
        invoice.periodEnd = anyDayInMonth.with(TemporalAdjusters.lastDayOfMonth()).minusDays(1);
        invoice.dueDate = invoice.periodEnd.plusDays(PAYMENT_TERM_DAYS);
    }

    public boolean isOverdue(Invoice invoice, LocalDate today) {
        return today.isAfter(invoice.dueDate);
    }
}
EOF
commit "$WU" "2026-03-04T14:30:00+08:00" "feat: 自然月账期和逾期判断"

# ---------------------------------------------------------------- 3. fixes on arithmetic and dates
cat > $PKG/service/InvoiceService.java <<'EOF'
package com.example.billing.service;

import com.example.billing.model.Invoice;
import java.math.BigDecimal;
import java.math.RoundingMode;
import java.util.Map;

public class InvoiceService {
    private static final Map<String, String> CURRENCY_ALIASES = Map.of(
        "人民币", "CNY", "RMB", "CNY", "CNY", "CNY",
        "美元", "USD", "USD", "USD");

    /** 含税金额 = (净额 - 折扣) × (1 + 税率)。先算完再统一舍入，避免中间步骤的舍入误差。 */
    public BigDecimal grossAmount(Invoice invoice) {
        BigDecimal base = invoice.netAmount.subtract(invoice.discount);
        BigDecimal gross = base.multiply(BigDecimal.ONE.add(invoice.taxRate));
        return gross.setScale(2, RoundingMode.HALF_UP);
    }

    public BigDecimal taxAmount(Invoice invoice) {
        return grossAmount(invoice).subtract(invoice.netAmount.subtract(invoice.discount)).setScale(2, RoundingMode.HALF_UP);
    }

    public String normalizeCurrency(String raw) {
        String code = CURRENCY_ALIASES.get(raw.trim().toUpperCase());
        if (code == null) {
            code = CURRENCY_ALIASES.get(raw.trim());
        }
        if (code == null) {
            throw new IllegalArgumentException("unknown currency: " + raw);
        }
        return code;
    }
}
EOF
commit "$ZHOU" "2026-04-11T09:20:00+08:00" "fix: 税额单独舍入导致含税金额差一分钱"

sed -i.bak 's/invoice.periodEnd = anyDayInMonth.with(TemporalAdjusters.lastDayOfMonth()).minusDays(1);/invoice.periodEnd = anyDayInMonth.with(TemporalAdjusters.lastDayOfMonth());/' $PKG/service/BillingPeriodService.java && rm $PKG/service/BillingPeriodService.java.bak
commit "$WU" "2026-05-06T16:10:00+08:00" "fix: 账期结束日少算一天"

# ---------------------------------------------------------------- 4. export + tests
cat > $PKG/export/CsvExporter.java <<'EOF'
package com.example.billing.export;

import com.example.billing.model.Invoice;
import java.util.List;
import java.util.stream.Collectors;

public class CsvExporter {
    private static final String HEADER = "id,customer_id,currency,net_amount,tax_rate,discount,period_start,period_end,due_date,status";

    public String export(List<Invoice> invoices) {
        String rows = invoices.stream()
            .map(i -> String.join(",",
                String.valueOf(i.id), i.customerId, i.currency,
                i.netAmount.toPlainString(), i.taxRate.toPlainString(), i.discount.toPlainString(),
                i.periodStart.toString(), i.periodEnd.toString(), i.dueDate.toString(), i.status.name()))
            .collect(Collectors.joining("\n"));
        return HEADER + "\n" + rows + "\n";
    }
}
EOF

cat > $TPKG/InvoiceServiceTest.java <<'EOF'
package com.example.billing;

import static org.junit.jupiter.api.Assertions.assertEquals;

import com.example.billing.model.Invoice;
import com.example.billing.service.InvoiceService;
import java.math.BigDecimal;
import org.junit.jupiter.api.Test;

class InvoiceServiceTest {
    private final InvoiceService service = new InvoiceService();

    @Test
    void grossAmountRoundsOnce() {
        Invoice i = new Invoice();
        i.netAmount = new BigDecimal("100.05");
        i.discount = new BigDecimal("0.00");
        i.taxRate = new BigDecimal("0.06");
        assertEquals(new BigDecimal("106.05"), service.grossAmount(i));
    }

    @Test
    void currencyAliases() {
        assertEquals("CNY", service.normalizeCurrency("人民币"));
        assertEquals("CNY", service.normalizeCurrency("rmb"));
        assertEquals("USD", service.normalizeCurrency("美元"));
    }
}
EOF
commit "$WU" "2026-06-02T11:45:00+08:00" "feat: 导出 CSV，补充金额与币种测试"

sed -i.bak 's/return today.isAfter(invoice.dueDate);/return invoice.dueDate != null \&\& today.isAfter(invoice.dueDate);/' $PKG/service/BillingPeriodService.java && rm $PKG/service/BillingPeriodService.java.bak
commit "$ZHOU" "2026-07-15T10:30:00+08:00" "fix: 草稿发票没有到期日时逾期判断空指针"
